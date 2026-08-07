"""Summarize CP0-CP6 results without collapsing automated and manual judgments."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from trt_core.experiment_evaluation import (
    CHECKPOINTS,
    auto_human_metrics,
    classify_outcome,
    completion_metrics,
    rate,
)


TRUE_VALUES = {"1", "true", "pass", "yes"}
FALSE_VALUES = {"0", "false", "fail", "no"}


def optional_bool(value: Any) -> bool | None:
    if value is True or value is False:
        return value
    normalized = str(value or "").strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return None


def load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def summarize(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        checkpoints = {
            cp: optional_bool(row.get(cp) if cp in row else row.get(f"{cp}_pass"))
            for cp in CHECKPOINTS
        }
        manual_correction = optional_bool(row.get("manual_correction_used")) is True
        operator_accepted = optional_bool(row.get("operator_accepted"))
        system_error = optional_bool(row.get("system_error")) is True
        row["outcome_class"] = row.get("outcome_class") or classify_outcome(
            checkpoints,
            manual_correction_used=manual_correction,
            operator_accepted=operator_accepted,
            system_error=system_error,
        )
        row["manual_intervention_required"] = (
            optional_bool(row.get("manual_intervention_required")) is True
            or manual_correction
        )
        row["automated_result"] = str(row.get("automated_result") or "").upper() or None
        row["manual_result"] = str(row.get("manual_result") or "").upper() or None
        row["checkpoints"] = checkpoints
        normalized.append(row)

    checkpoint_metrics: dict[str, Any] = {}
    for cp, definition in CHECKPOINTS.items():
        entered = [row for row in normalized if row["checkpoints"][cp] is not None]
        passed = sum(row["checkpoints"][cp] is True for row in entered)
        checkpoint_metrics[cp] = {
            **definition,
            "entered": len(entered),
            "passed": passed,
            "pass_rate": rate(passed, len(entered)),
        }
    summary = {
        "rows": len(normalized),
        "checkpoint_metrics": checkpoint_metrics,
        "completion_metrics": completion_metrics(normalized),
        "auto_human_metrics": auto_human_metrics(normalized),
        "outcome_counts": dict(Counter(row["outcome_class"] for row in normalized)),
        "failure_cause_counts": dict(
            Counter(
                str(row.get("failure_cause"))
                for row in normalized
                if row.get("failure_cause")
            )
        ),
    }
    return normalized, summary


def write_outputs(output: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "checkpoint_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    disagreements = [
        row
        for row in rows
        if row.get("automated_result") in {"PASS", "FAIL"}
        and row.get("manual_result") in {"PASS", "FAIL"}
        and row["automated_result"] != row["manual_result"]
    ]
    fields = [
        "test_id",
        "automated_result",
        "manual_result",
        "outcome_class",
        "failure_stage",
        "failure_cause",
        "disagreement_reason",
        "correction_method",
    ]
    with (output / "auto_human_disagreements.csv").open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(disagreements)
    lines = [
        "# Checkpoint and Outcome Summary",
        "",
        f"Cases: `{summary['rows']}`",
        "",
        "## Checkpoints",
        "",
        "| Checkpoint | Entered | Passed | Pass rate |",
        "| --- | ---: | ---: | ---: |",
    ]
    for cp, metric in summary["checkpoint_metrics"].items():
        value = metric["pass_rate"]
        lines.append(
            f"| {cp} | {metric['entered']} | {metric['passed']} | "
            f"{'DATA_INCOMPLETE' if value is None else f'{value:.3f}'} |"
        )
    lines.extend(["", "## Outcome Counts", ""])
    lines.extend(
        f"- `{name}`: {count}"
        for name, count in sorted(summary["outcome_counts"].items())
    )
    lines.extend(
        [
            "",
            "Automated and manual decisions are reported separately. "
            "A manually assisted completion is not counted as autonomous success.",
        ]
    )
    (output / "checkpoint_summary.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows, summary = summarize(load_rows(args.input))
    write_outputs(args.output, rows, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
