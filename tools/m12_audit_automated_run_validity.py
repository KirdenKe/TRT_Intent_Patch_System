from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from trt_core.repository import PROJECT_ROOT


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_manual_packet(packet_dir: Path) -> dict[str, dict[str, str]]:
    files = [
        "tc1_intent_plan_manual.csv",
        "tc2_tool_orchestration_manual.csv",
        "tc3_kpi_report_manual.csv",
        "tc4_error_interception_manual.csv",
    ]
    rows: dict[str, dict[str, str]] = {}
    for filename in files:
        for row in read_csv(packet_dir / filename):
            rows[row["test_id"]] = row
    return rows


def load_combined(path: str) -> dict[str, Any]:
    if not path:
        return {}
    candidate = Path(path)
    if not candidate.exists():
        return {}
    return json.loads(candidate.read_text(encoding="utf-8"))


def turn_messages(combined: dict[str, Any]) -> list[str]:
    turns = combined.get("turns") if isinstance(combined, dict) else []
    if not isinstance(turns, list):
        return []
    return [str(turn.get("message") or "") for turn in turns]


def turn_text(combined: dict[str, Any]) -> str:
    turns = combined.get("turns") if isinstance(combined, dict) else []
    texts: list[str] = []
    if isinstance(turns, list):
        for turn in turns:
            texts.append(str(turn.get("text") or ""))
    return "\n".join(texts)


def validity_for_row(result: dict[str, str], manual: dict[str, str], combined: dict[str, Any]) -> tuple[str, str, str]:
    suite = result.get("suite", "")
    messages = turn_messages(combined)
    text = turn_text(combined).lower()
    prompt = messages[0] if messages else ""
    status = result.get("status", "")

    if suite == "TC4":
        feasibility = manual.get("manual_feasibility", "")
        if feasibility == "REQUIRES_BACKEND_INJECTION":
            return (
                "INVALID_AUTOMATION",
                "TC4_BACKEND_INJECTION_NOT_PERFORMED",
                "Manual packet requires backend/state injection, but the automated runner only submitted chat turns. Empty or generic chat prompts cannot validate this error class.",
            )
        if manual.get("paste_into_n8n", "") and prompt.strip() != manual.get("paste_into_n8n", "").strip():
            return (
                "INVALID_AUTOMATION",
                "TC4_PROMPT_MISMATCH",
                "The prompt sent to n8n does not match the TC4 manual packet.",
            )
        if "candidate patch passed validation" in text and manual.get("approval_reply", "").lower().startswith("do not approve"):
            return (
                "VALID_EXECUTION_FAILED_EXPECTATION",
                "TC4_REACHED_CANDIDATE_APPROVAL",
                "The chat-prompt TC4 row was executed, but the system reached candidate approval where the packet expected rejection, clarification, or blocking.",
            )
        if "needs revision" in text or "please revise" in text or "cannot be processed" in text:
            return (
                "VALID_EXECUTION_PASS",
                "TC4_REJECTED_OR_REVISION_REQUESTED",
                "The chat-prompt TC4 row stopped before approval/deployment with a rejection or revision request.",
            )
        if "still need" in text:
            return (
                "VALID_EXECUTION_CLARIFICATION",
                "TC4_REQUIRED_FIELD_CLARIFICATION",
                "The workflow asked for missing required fields. This is normal process behavior, not enough by itself to prove the intended interceptor.",
            )
        return (
            "VALID_EXECUTION_INCONCLUSIVE",
            "TC4_NO_STRUCTURED_INTERCEPTOR_OBSERVED",
            "A chat-prompt TC4 row ran, but the transcript did not expose the expected interceptor or deployment-block outcome clearly.",
        )

    if suite == "TC2":
        if status == "PASS" and ("still need operator" in text or "before i can submit this for review" in text or "cannot perform calculations" in text):
            return (
                "INVALID_SCORING",
                "TC2_PASS_INFERRED_FROM_ANY_RESPONSE",
                "Runner marked TC2 PASS because any response text existed; it did not verify required tools, order, arguments, or whether the answer was actually produced.",
            )
        return (
            "NEEDS_RESCORING",
            "TC2_TOOL_TRACE_NOT_VERIFIED",
            "TC2 requires tool sequence and argument comparison. The runner did not capture/score tool traces.",
        )

    if suite == "TC1":
        expected_status = manual.get("expected_status", "")
        if expected_status in {"REJECTED", "NEEDS_CLARIFICATION"} and status in {"PASS", "FAIL_ERROR_NOT_INTERCEPTED"}:
            return (
                "NEEDS_RESCORING",
                "TC1_EXPECTED_NEGATIVE_CASE_REACHED_APPROVAL_OR_PASS",
                "TC1 negative rows must be judged against expected rejection/clarification semantics, not only run_id presence.",
            )
        return (
            "PARTIAL_VALID_RAW_EXECUTION",
            "TC1_EXPECTED_FIELDS_NOT_VALIDATED",
            "Raw chat/ScenarioSpec/RunArtifact data are useful, but runner did not compare actual candidate patch or ScenarioSpec fields against expected_fields_json.",
        )

    if suite == "TC3":
        return (
            "PARTIAL_VALID_RAW_EXECUTION",
            "TC3_CONSTRAINTS_NOT_VALIDATED",
            "Raw live metric rows are useful, but runner did not validate each setup's expected constraints beyond run completion.",
        )

    return ("UNKNOWN", "UNKNOWN_SUITE", "No validity rule for this suite.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit validity of automated M12 full-run scoring against manual packet semantics.")
    parser.add_argument("--run-dir", default="outputs/reports/m12/automated_full_n8n_run_20260703_serial")
    parser.add_argument("--packet", default="outputs/reports/m12/manual_test_packet")
    parser.add_argument("--output", default="outputs/reports/m12/comparison_results/automated_run_validity_audit")
    args = parser.parse_args()

    run_dir = PROJECT_ROOT / args.run_dir if not Path(args.run_dir).is_absolute() else Path(args.run_dir)
    packet_dir = PROJECT_ROOT / args.packet if not Path(args.packet).is_absolute() else Path(args.packet)
    out_dir = PROJECT_ROOT / args.output if not Path(args.output).is_absolute() else Path(args.output)

    manual_rows = load_manual_packet(packet_dir)
    result_rows = read_csv(run_dir / "full_n8n_results_latest.csv")
    audit_rows: list[dict[str, Any]] = []
    for result in result_rows:
        manual = manual_rows.get(result["test_id"], {})
        combined = load_combined(result.get("combined_execution_json", ""))
        validity, issue_code, rationale = validity_for_row(result, manual, combined)
        audit_rows.append(
            {
                "test_id": result.get("test_id"),
                "suite": result.get("suite"),
                "raw_status": result.get("status"),
                "manual_feasibility": manual.get("manual_feasibility", ""),
                "expected_status": manual.get("expected_status", ""),
                "expected_interceptor": manual.get("expected_interceptor", ""),
                "validity_status": validity,
                "issue_code": issue_code,
                "rationale": rationale,
                "run_id": result.get("run_id"),
                "scenario_spec_id": result.get("scenario_spec_id"),
                "combined_execution_json": result.get("combined_execution_json"),
            }
        )

    fields = [
        "test_id",
        "suite",
        "raw_status",
        "manual_feasibility",
        "expected_status",
        "expected_interceptor",
        "validity_status",
        "issue_code",
        "rationale",
        "run_id",
        "scenario_spec_id",
        "combined_execution_json",
    ]
    write_csv(out_dir / "m12_automated_run_validity_audit.csv", audit_rows, fields)

    suite_counts = Counter((row["suite"], row["validity_status"]) for row in audit_rows)
    issue_counts = Counter(row["issue_code"] for row in audit_rows)
    summary = {
        "total_rows": len(audit_rows),
        "suite_validity_counts": {f"{suite}:{status}": count for (suite, status), count in suite_counts.items()},
        "issue_counts": dict(issue_counts),
        "high_level_conclusion": {
            "TC1": "Raw executions are partially useful, but pass/fail correctness must be rescored against expected candidate/ScenarioSpec fields.",
            "TC2": "Automated scoring is invalid for pass-rate claims because the runner did not verify required tools/order/arguments.",
            "TC3": "Live metric rows are useful, but setup-level constraint satisfaction must be rescored.",
            "TC4": "Most TC4 rows are invalid as automated interception tests because backend-injection rows were not injected and chat rows need semantic rescoring.",
        },
    }
    (out_dir / "m12_automated_run_validity_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    md = [
        "# M12 Automated Run Validity Audit",
        "",
        "This audit compares the completed automated run against `outputs/reports/m12/manual_test_packet/*.csv`.",
        "",
        "## Conclusion",
        "",
        "- **TC4 must be retested.** Rows marked `REQUIRES_BACKEND_INJECTION` were not actually injected; the runner only submitted chat turns.",
        "- **TC2 pass-rate claims are invalid.** The runner marked TC2 rows as PASS when any response existed, without validating required tools, order, or arguments.",
        "- **TC1 and TC3 raw live data remain useful for timing/RunArtifact metrics, but correctness claims need rescoring.** The runner did not compare candidate patches, ScenarioSpecs, or setup constraints against expected fields.",
        "",
        "## Suite Validity Counts",
        "",
    ]
    for key, count in sorted(summary["suite_validity_counts"].items()):
        md.append(f"- `{key}`: {count}")
    md.extend(["", "## Issue Counts", ""])
    for key, count in sorted(summary["issue_counts"].items()):
        md.append(f"- `{key}`: {count}")
    md.extend(
        [
            "",
            "## Recommended Retest Plan",
            "",
            "1. Retest TC4 with two paths: chat-prompt rows through n8n, backend-injection rows through deterministic stage-specific injection harnesses.",
            "2. Rescore TC2 using captured route/tool traces or deterministic backend function traces.",
            "3. Rescore TC1 by comparing actual candidate patch and ScenarioSpec fields against `expected_fields_json`.",
            "4. Rescore TC3 setup constraints while preserving existing live RunArtifact timing/placement metrics where provenance is valid.",
        ]
    )
    (out_dir / "m12_automated_run_validity_audit.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({"status": "OK", "output": str(out_dir), **summary}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
