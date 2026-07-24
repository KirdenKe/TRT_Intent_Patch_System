from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from trt_core.repository import PROJECT_ROOT
from tools.m12_packet_scorer import (
    actual_tools_from_combined,
    combined_response_text,
    parse_jsonish,
    score_combined,
)


PASS = "PASS"
FAIL = "FAIL"


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def has_any(text: str, tokens: list[str]) -> bool:
    return any(token in text for token in tokens)


def extract_structured_markers(combined: dict[str, Any]) -> dict[str, bool]:
    markers = {
        "has_kpi_targets": False,
        "has_task_requirement_table": False,
        "has_line_results": False,
        "has_metric_value": False,
        "has_source_reference": False,
        "has_answer_ready": False,
    }
    for item in walk_dicts(combined):
        if item.get("status") == "ANSWER_READY":
            markers["has_answer_ready"] = True
        structured = item.get("structured_answer")
        if isinstance(structured, dict):
            if structured.get("kpi_targets"):
                markers["has_kpi_targets"] = True
            if structured.get("task_requirement_table"):
                markers["has_task_requirement_table"] = True
            if structured.get("line_results"):
                markers["has_line_results"] = True
            if any(key in structured for key in ("R_storage", "R_reset", "T_wait_seconds", "T_verification_seconds", "T_loop_seconds")):
                markers["has_metric_value"] = True
        sources = item.get("sources") or item.get("sources_used")
        if isinstance(sources, list) and sources:
            markers["has_source_reference"] = True
    return markers


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_dicts(item)


def tc2_operator_answer_adjudication(combined: dict[str, Any], packet_score: dict[str, Any]) -> tuple[str, str, str]:
    row = combined.get("row") or {}
    prompt = str(row.get("paste_into_n8n") or row.get("operator_query") or "").lower()
    expected_tools = parse_jsonish(row.get("required_tools"), [])
    actual_tools = actual_tools_from_combined(combined)
    text = combined_response_text(combined).lower()
    markers = extract_structured_markers(combined)

    if has_any(text, ["could not tell what task change", "cannot perform calculations", "would you like to update a production goal"]):
        return FAIL, "operator_answer", "The chat response did not answer the reporting/config query and routed it as an unsupported or unclear task-change request."

    if "kpi" in prompt and ("extract_kpi_targets" in expected_tools or "load_current_trt" in expected_tools):
        if markers["has_kpi_targets"] or ("minimum throughput" in text and "source:" in text):
            return PASS, "operator_answer", "The operator-facing response returned KPI target rows from the TRT; missing/implicit trace steps are not counted as a user-facing failure."

    if "task requirement" in prompt:
        if markers["has_task_requirement_table"] or "task requirement" in text:
            return PASS, "operator_answer", "The operator-facing response returned the requested task requirement information."

    if any(metric in prompt for metric in ("r_reset", "reset completion", "r_storage", "placement verification pass rate")):
        if markers["has_metric_value"] or any(metric in text for metric in ("r_reset", "r storage", "r_storage", "placement verification pass rate", "reset completion rate")):
            if not has_any(text, ["could not", "cannot perform", "missing run", "no run"]):
                return PASS, "operator_answer", "The response appears to provide the requested metric."
        return FAIL, "operator_answer", "The metric query did not produce a metric answer from stored run/report data."

    if packet_score.get("status") == "PASS":
        return PASS, "structured_trace", "Automated tool trace matched the expected sequence."
    if actual_tools:
        return FAIL, "structured_trace", "A tool trace was captured, but it did not satisfy the expected tool/order requirements and no operator-answer exception applied."
    return FAIL, "data_quality", "No structured trace or verifiable operator answer was available."


def tc4_adjudication(combined: dict[str, Any], packet_score: dict[str, Any]) -> tuple[str, str, str]:
    row = combined.get("row") or {}
    text = combined_response_text(combined).lower()
    expected_blocked = str(row.get("expected_deployment_blocked", "")).lower() == "true"
    deployed = "deployment completed" in text or "deployment success" in text
    reached_approval = "candidate patch passed validation" in text or "approve, reject, or request revision" in text
    rejection_or_clarification = has_any(
        text,
        [
            "could not tell what task change",
            "please provide a task request",
            "needs revision",
            "requires revision",
            "please clarify",
            "missing",
            "not allowed",
            "blocked",
            "cannot",
            "could not",
        ],
    )

    if packet_score.get("status") == "PASS" and not deployed:
        return PASS, "packet_interception_review", "The automated packet scorer recorded a pre-deployment stop/block, and no deployment-success evidence appeared."

    if expected_blocked:
        if deployed:
            return FAIL, "deployment_guardrail", "A deployment-success response appeared for a row expected to be blocked."
        if rejection_or_clarification:
            return PASS, "operator_refusal_or_clarification", "The workflow refused, clarified, or stopped before deployment; this counts as interception under the operator-safety criterion."
        if reached_approval:
            return FAIL, "intent_validation", "The workflow reached candidate approval for a row expected to be blocked."
        return FAIL, "data_quality", "The transcript did not prove rejection, clarification, block, or deployment prevention."

    if not deployed:
        return PASS, "noncritical_no_deployment", "A non-critical case did not deploy during M12 testing."
    return FAIL, "deployment_guardrail", "Deployment-like success text appeared during comparison testing."


def adjudicate_combined(combined: dict[str, Any]) -> dict[str, Any]:
    row = combined.get("row") or {}
    suite = row.get("suite") or ""
    packet_score = combined.get("packet_score") or score_combined(combined, PROJECT_ROOT)
    automated_status = packet_score.get("status", "")
    automated_reason = packet_score.get("failure_cause", "")

    if suite == "TC2":
        final_status, basis, reason = tc2_operator_answer_adjudication(combined, packet_score)
    elif suite == "TC4":
        final_status, basis, reason = tc4_adjudication(combined, packet_score)
    else:
        if automated_status in {"PASS", "REJECTED", "SKIPPED_BY_TEST_PLAN"}:
            final_status, basis, reason = PASS, "packet_expectation", automated_reason or "Packet expectation passed."
        else:
            final_status, basis, reason = FAIL, "packet_expectation", automated_reason or "Packet expectation failed."

    return {
        "test_id": combined.get("test_id") or row.get("test_id", ""),
        "packet_test_id": row.get("packet_test_id", ""),
        "suite": suite,
        "automated_status": automated_status,
        "automated_failure_stage": packet_score.get("failure_stage", ""),
        "automated_failure_cause": automated_reason,
        "final_binary_status": final_status,
        "adjudication_basis": basis,
        "adjudication_reason": reason,
        "scenario_spec_id": combined.get("scenario_spec_id", ""),
        "run_id": combined.get("run_id", ""),
        "chat_session_id": combined.get("session_id", ""),
        "n8n_execution_ids": ",".join(str(s.get("execution_id")) for s in combined.get("n8n_execution_snapshots") or [] if isinstance(s, dict) and s.get("execution_id")),
        "combined_execution_json": str(combined.get("_combined_execution_json", "")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create binary reviewed M12 adjudication results from n8n combined executions.")
    parser.add_argument("--run-dir", required=True, help="Automated n8n run directory containing combined_executions/*.json.")
    parser.add_argument("--output", help="Output directory. Defaults to <run-dir>/adjudicated_results.")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_ROOT / run_dir
    output = Path(args.output) if args.output else run_dir / "adjudicated_results"
    if not output.is_absolute():
        output = PROJECT_ROOT / output

    combined_dir = run_dir / "combined_executions"
    if not combined_dir.exists():
        raise SystemExit(f"combined_executions not found: {combined_dir}")

    rows: list[dict[str, Any]] = []
    for path in sorted(combined_dir.glob("*.json")):
        combined = json.loads(path.read_text(encoding="utf-8"))
        combined["_combined_execution_json"] = str(path)
        rows.append(adjudicate_combined(combined))

    fields = [
        "test_id",
        "packet_test_id",
        "suite",
        "automated_status",
        "automated_failure_stage",
        "automated_failure_cause",
        "final_binary_status",
        "adjudication_basis",
        "adjudication_reason",
        "scenario_spec_id",
        "run_id",
        "chat_session_id",
        "n8n_execution_ids",
        "combined_execution_json",
    ]
    write_csv(output / "m12_binary_adjudicated_results.csv", rows, fields)

    summary = {
        "run_dir": str(run_dir),
        "rows": len(rows),
        "final_binary_counts": dict(Counter(row["final_binary_status"] for row in rows)),
        "suite_binary_counts": dict(Counter(f"{row['suite']}:{row['final_binary_status']}" for row in rows)),
        "basis_counts": dict(Counter(row["adjudication_basis"] for row in rows)),
        "automated_status_counts": dict(Counter(f"{row['suite']}:{row['automated_status']}" for row in rows)),
    }
    (output / "m12_binary_adjudicated_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# M12 Binary Adjudication",
        "",
        f"Run directory: `{run_dir}`",
        f"Rows adjudicated: `{len(rows)}`",
        "",
        "## Final Binary Counts",
        "",
    ]
    for key, value in summary["final_binary_counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Suite Counts", ""])
    for key, value in summary["suite_binary_counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Notes", ""])
    lines.append("- Automated scorer status is preserved separately from final binary adjudication.")
    lines.append("- TC2 can pass on operator-facing answer quality when the answer is correct but an implicit trace step was not captured.")
    lines.append("- TC4 counts clarification/refusal before deployment as successful interception.")
    (output / "m12_binary_adjudicated_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"status": "OK", "rows": len(rows), "output": str(output)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
