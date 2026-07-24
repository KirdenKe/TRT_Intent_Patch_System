from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trt_core.repository import PROJECT_ROOT


M12_ROOT = PROJECT_ROOT / "outputs" / "reports" / "m12"

PACKET_FILES = {
    "TC1": "tc1_intent_plan_manual.csv",
    "TC2": "tc2_tool_orchestration_manual.csv",
    "TC3": "tc3_kpi_report_manual.csv",
    "TC4": "tc4_error_interception_manual.csv",
}

REQUIRED_COLUMNS = {
    "TC1": {
        "test_id",
        "paste_into_n8n",
        "operator_details_reply",
        "approval_reply",
        "expected_status",
        "expected_fields_json",
    },
    "TC2": {
        "test_id",
        "depth",
        "paste_into_n8n",
        "required_tools",
        "required_order",
        "required_arguments",
    },
    "TC3": {
        "test_id",
        "paste_into_n8n",
        "approval_reply",
        "expected_run_id",
        "expected_fields_json",
    },
    "TC4": {
        "test_id",
        "injected_error_type",
        "manual_feasibility",
        "paste_into_n8n",
        "expected_interceptor",
        "expected_deployment_blocked",
    },
}

EXPECTED_ROW_COUNTS = {
    "TC1": 44,
    "TC2": 75,
    "TC3": 30,
    "TC4": 25,
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def packet_audit(packet_dir: Path) -> dict[str, Any]:
    suites: dict[str, Any] = {}
    findings: list[dict[str, str]] = []
    for suite, filename in PACKET_FILES.items():
        path = packet_dir / filename
        rows = read_csv(path)
        columns = set(rows[0].keys()) if rows else set()
        missing_columns = sorted(REQUIRED_COLUMNS[suite] - columns)
        row_count = len(rows)
        expected_count = EXPECTED_ROW_COUNTS[suite]
        suites[suite] = {
            "file": str(path),
            "exists": path.exists(),
            "row_count": row_count,
            "expected_row_count": expected_count,
            "columns": sorted(columns),
            "missing_required_columns": missing_columns,
        }
        if not path.exists():
            findings.append({"severity": "FATAL", "code": f"{suite}_PACKET_MISSING", "message": f"{filename} is missing."})
        if row_count != expected_count:
            findings.append(
                {
                    "severity": "FATAL",
                    "code": f"{suite}_ROW_COUNT_MISMATCH",
                    "message": f"{suite} has {row_count} rows; expected {expected_count}.",
                }
            )
        if missing_columns:
            findings.append(
                {
                    "severity": "FATAL",
                    "code": f"{suite}_MISSING_PACKET_COLUMNS",
                    "message": f"{suite} is missing required packet columns: {', '.join(missing_columns)}.",
                }
            )

    tc4_rows = read_csv(packet_dir / PACKET_FILES["TC4"])
    feasibility_counts: dict[str, int] = {}
    for row in tc4_rows:
        key = row.get("manual_feasibility", "") or "UNKNOWN"
        feasibility_counts[key] = feasibility_counts.get(key, 0) + 1
    suites["TC4"]["manual_feasibility_counts"] = feasibility_counts
    return {"suites": suites, "findings": findings}


def plan_audit(plan_dir: Path) -> dict[str, Any]:
    path = plan_dir / "full_isaac_parameter_plan.csv"
    rows = read_csv(path)
    findings: list[dict[str, str]] = []
    distribution: dict[str, int] = {}
    invalid_rows: list[dict[str, str]] = []
    for row in rows:
        try:
            total_tooling = int(float(row.get("total_tooling", "0") or 0))
            num_envs = int(float(row.get("num_envs", "0") or 0))
        except ValueError:
            invalid_rows.append(row)
            continue
        distribution[f"total_tooling_{total_tooling}_num_envs_{num_envs}"] = distribution.get(
            f"total_tooling_{total_tooling}_num_envs_{num_envs}", 0
        ) + 1
        if total_tooling > 12 or total_tooling not in {8, 10, 12} or num_envs > 4:
            invalid_rows.append(row)
        if total_tooling == 10 and num_envs == 4:
            invalid_rows.append(row)
    if not path.exists():
        findings.append({"severity": "FATAL", "code": "FULL_PARAMETER_PLAN_MISSING", "message": "full_isaac_parameter_plan.csv is missing."})
    if invalid_rows:
        findings.append(
            {
                "severity": "FATAL",
                "code": "FULL_PARAMETER_PLAN_CONSTRAINT_VIOLATION",
                "message": f"{len(invalid_rows)} launch rows violate the tooling/line constraints.",
            }
        )
    return {
        "file": str(path),
        "exists": path.exists(),
        "row_count": len(rows),
        "distribution": distribution,
        "invalid_row_count": len(invalid_rows),
        "findings": findings,
    }


def runner_audit(runner_path: Path) -> dict[str, Any]:
    source = runner_path.read_text(encoding="utf-8") if runner_path.exists() else ""
    scorer_path = runner_path.with_name("m12_packet_scorer.py")
    scorer_source = scorer_path.read_text(encoding="utf-8") if scorer_path.exists() else ""
    scoring_source = source + "\n" + scorer_source
    wait_execution_call_count = source.count("wait_execution(")
    fetch_snapshot_call_count = source.count("fetch_execution_snapshots(")
    findings: list[dict[str, str]] = []
    checks = {
        "loads_manual_packet": "tc1_intent_plan_manual.csv" in source and "tc4_error_interception_manual.csv" in source,
        "tc2_heuristic_pass_if_any_text": 'test_id.startswith("TC2-") and text.strip()' in source,
        "approved_run_id_pass_heuristic": 'approved and run_id' in source and 'return "PASS"' in source,
        "packet_scorer_wired": "score_combined" in source,
        "tc4_backend_injection_branch": "run_tc4_backend_injection" in source,
        "tc1_expected_fields_json_scored": "expected_fields_json" in scoring_source and "kpi_update_match" in scoring_source,
        "tc2_required_tools_scored": "required_tools" in scoring_source and "dependency_order_correct" in scoring_source,
        "tc3_constraints_scored": "expected_constraints" in scoring_source and "data_quality_status" in scoring_source,
        "structured_n8n_execution_data_captured": "includeData=true" in source and fetch_snapshot_call_count > 1,
        "wait_execution_call_count": wait_execution_call_count,
        "fetch_execution_snapshots_call_count": fetch_snapshot_call_count,
    }
    if not runner_path.exists():
        findings.append({"severity": "FATAL", "code": "RUNNER_MISSING", "message": f"{runner_path} is missing."})
    if checks["tc2_heuristic_pass_if_any_text"]:
        findings.append(
            {
                "severity": "FATAL",
                "code": "TC2_HEURISTIC_SCORING",
                "message": "TC2 is scored as PASS when any response text exists; required tools/order/arguments are not validated.",
            }
        )
    if checks["approved_run_id_pass_heuristic"]:
        findings.append(
            {
                "severity": "FATAL",
                "code": "TC1_TC3_RUN_ID_ONLY_SCORING",
                "message": "Approved TC1/TC3 rows can be scored PASS from run_id presence without expected field validation.",
            }
        )
    if not checks["tc4_backend_injection_branch"]:
        findings.append(
            {
                "severity": "FATAL",
                "code": "TC4_BACKEND_INJECTION_NOT_IMPLEMENTED",
                "message": "The runner has no branch for manual_feasibility=REQUIRES_BACKEND_INJECTION.",
            }
        )
    if not checks["structured_n8n_execution_data_captured"]:
        findings.append(
            {
                "severity": "WARNING",
                "code": "STRUCTURED_TRACE_CAPTURE_MISSING",
                "message": "The runner does not fetch n8n includeData execution payloads, so candidate patches/tool traces may be unavailable for scoring.",
            }
        )
    return {"file": str(runner_path), "exists": runner_path.exists(), "checks": checks, "findings": findings}


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# M12 Full-Test Readiness Check",
        "",
        f"Created at: {payload['created_at_utc']}",
        "",
        f"Overall status: **{payload['overall_status']}**",
        "",
        "## Conclusion",
        "",
        payload["conclusion"],
        "",
        "## Findings",
        "",
    ]
    for finding in payload["findings"]:
        lines.append(f"- `{finding['severity']}` `{finding['code']}`: {finding['message']}")
    lines.extend(["", "## Packet Row Counts", ""])
    for suite, info in payload["packet"]["suites"].items():
        lines.append(f"- `{suite}`: {info['row_count']} rows, missing columns: {info['missing_required_columns']}")
        if suite == "TC4":
            lines.append(f"  TC4 feasibility counts: `{info.get('manual_feasibility_counts', {})}`")
    lines.extend(["", "## Isaac Parameter Plan", ""])
    plan = payload["plan"]
    lines.append(f"- rows: `{plan['row_count']}`")
    lines.append(f"- invalid row count: `{plan['invalid_row_count']}`")
    lines.append(f"- distribution: `{plan['distribution']}`")
    lines.extend(["", "## Runner Checks", ""])
    for key, value in payload["runner"]["checks"].items():
        lines.append(f"- `{key}`: `{value}`")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether M12 full-test automation is ready for trusted scoring.")
    parser.add_argument("--packet", default=str(M12_ROOT / "manual_test_packet"))
    parser.add_argument("--plan", default=str(M12_ROOT / "full_test_plan"))
    parser.add_argument("--runner", default=str(PROJECT_ROOT / "tools" / "m12_run_full_n8n_tests.py"))
    parser.add_argument("--output", default=str(M12_ROOT / "comparison_results" / "full_test_readiness"))
    args = parser.parse_args()

    packet = packet_audit(Path(args.packet))
    plan = plan_audit(Path(args.plan))
    runner = runner_audit(Path(args.runner))
    findings = packet["findings"] + plan["findings"] + runner["findings"]
    fatal_count = sum(1 for finding in findings if finding["severity"] == "FATAL")
    overall_status = "BLOCKED" if fatal_count else "READY"
    conclusion = (
        "Do not run a new full test for final scoring yet. The packet and launch plan are present, but the runner still uses heuristic scoring and does not implement TC4 backend injection."
        if overall_status == "BLOCKED"
        else "The packet, launch plan, and runner checks passed."
    )
    payload = {
        "created_at_utc": now_utc(),
        "overall_status": overall_status,
        "can_run_full_again_for_trusted_scoring": overall_status == "READY",
        "conclusion": conclusion,
        "packet": packet,
        "plan": plan,
        "runner": runner,
        "findings": findings,
    }
    output = Path(args.output)
    write_json(output / "m12_full_test_readiness.json", payload)
    (output / "m12_full_test_readiness.md").write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps({"status": overall_status, "fatal_count": fatal_count, "output": str(output)}, indent=2, sort_keys=True))
    return 1 if overall_status == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
