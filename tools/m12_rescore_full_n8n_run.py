from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from trt_core.repository import PROJECT_ROOT
from tools.m12_packet_scorer import score_combined
from tools.m12_tc4_backend_injection import run_tc4_backend_injection


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rescore an M12 full n8n run using packet expectations.")
    parser.add_argument("--run-dir", default="outputs/reports/m12/automated_full_n8n_run_20260703_serial")
    parser.add_argument("--output", default="outputs/reports/m12/comparison_results/rescored_full_run")
    parser.add_argument("--run-backend-injections", action="store_true")
    args = parser.parse_args()

    run_dir = PROJECT_ROOT / args.run_dir if not Path(args.run_dir).is_absolute() else Path(args.run_dir)
    output = PROJECT_ROOT / args.output if not Path(args.output).is_absolute() else Path(args.output)
    combined_dir = run_dir / "combined_executions"
    rows: list[dict[str, Any]] = []
    for path in sorted(combined_dir.glob("*.json")):
        combined = json.loads(path.read_text(encoding="utf-8"))
        row = combined.get("row") or {}
        if args.run_backend_injections and row.get("suite") == "TC4" and row.get("manual_feasibility") == "REQUIRES_BACKEND_INJECTION":
            combined["backend_injection_result"] = run_tc4_backend_injection(row, output_root=output / "tc4_backend_injection")
        score = score_combined(combined, PROJECT_ROOT)
        rows.append(
            {
                "test_id": combined.get("test_id"),
                "suite": row.get("suite"),
                "old_status": combined.get("status"),
                "rescored_status": score.get("status"),
                "failure_stage": score.get("failure_stage"),
                "failure_cause": score.get("failure_cause"),
                "data_quality_status": score.get("data_quality_status"),
                "scoring_method": score.get("scoring_method"),
                "scenario_spec_id": combined.get("scenario_spec_id", ""),
                "run_id": combined.get("run_id", ""),
                "manual_feasibility": row.get("manual_feasibility", ""),
                "combined_execution_json": str(path),
            }
        )
    fields = [
        "test_id",
        "suite",
        "old_status",
        "rescored_status",
        "failure_stage",
        "failure_cause",
        "data_quality_status",
        "scoring_method",
        "scenario_spec_id",
        "run_id",
        "manual_feasibility",
        "combined_execution_json",
    ]
    write_csv(output / "m12_full_run_rescored.csv", rows, fields)
    summary = {
        "run_dir": str(run_dir),
        "rows": len(rows),
        "run_backend_injections": args.run_backend_injections,
        "status_counts": dict(Counter(f"{row['suite']}:{row['rescored_status']}" for row in rows)),
        "data_quality_counts": dict(Counter(str(row["data_quality_status"]) for row in rows)),
    }
    (output / "m12_full_run_rescored_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# M12 Full Run Rescore",
        "",
        f"Run directory: `{run_dir}`",
        f"Rows rescored: `{len(rows)}`",
        f"Backend injections executed: `{args.run_backend_injections}`",
        "",
        "## Status Counts",
        "",
    ]
    for key, value in summary["status_counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Data Quality Counts", ""])
    for key, value in summary["data_quality_counts"].items():
        lines.append(f"- `{key}`: {value}")
    (output / "m12_full_run_rescored.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": "OK", "rows": len(rows), "output": str(output)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
