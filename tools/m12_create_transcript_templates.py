from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from trt_core.repository import PROJECT_ROOT


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_manual_rows(packet_dir: Path) -> list[dict[str, str]]:
    files = [
        "tc1_intent_plan_manual.csv",
        "tc2_tool_orchestration_manual.csv",
        "tc3_kpi_report_manual.csv",
        "tc4_error_interception_manual.csv",
    ]
    rows: list[dict[str, str]] = []
    for name in files:
        path = packet_dir / name
        if path.exists():
            rows.extend(read_csv(path))
    return rows


def template_text(row: dict[str, Any]) -> str:
    test_id = row.get("test_id", "")
    expected = {
        key: row.get(key)
        for key in [
            "expected_status",
            "expected_fields_json",
            "required_tools",
            "required_order",
            "required_arguments",
            "expected_interceptor",
            "expected_deployment_blocked",
        ]
        if row.get(key)
    }
    return "\n".join(
        [
            f"TEST_ID: {test_id}",
            "RECORDED_BY:",
            "DATE_UTC:",
            "STATUS: <PASS|FAIL|REJECTED|SIMULATION_FAILED|INCONCLUSIVE|FAIL_ERROR_NOT_INTERCEPTED|WORKFLOW_LOOP|EVIDENCE_SUMMARY_MISSING>",
            "",
            "IDS",
            "CHAT_SESSION_ID:",
            "N8N_EXECUTION_ID:",
            "SCENARIO_SPEC_ID:",
            "RUN_ID:",
            "OUTPUT_DB_PATH:",
            "",
            "LIFECYCLE_TIMESTAMPS_UTC",
            "INTENT_CREATED_AT:",
            "SUMMARY_CREATED_AT:",
            "CANDIDATE_REVIEW_END_AT:",
            "SCENARIO_CREATED_AT:",
            "ARTIFACT_CREATED_AT:",
            "DEPLOYMENT_REVIEW_END_AT:",
            "",
            "MANUAL INSTRUCTIONS",
            f"PASTE_INTO_N8N: {row.get('paste_into_n8n', '')}",
            f"OPERATOR_DETAILS_REPLY: {row.get('operator_details_reply', '')}",
            f"APPROVAL_REPLY: {row.get('approval_reply', '')}",
            f"STOP_POINT: {row.get('stop_point', '')}",
            f"RECORD_STATUS_HINT: {row.get('record_status_hint', '')}",
            "",
            "EXPECTED CHECKS JSON",
            json.dumps(expected, indent=2, sort_keys=True),
            "",
            "CHAT TRANSCRIPT",
            "USER:",
            row.get("paste_into_n8n", ""),
            "",
            "N8N:",
            "<paste n8n response here>",
            "",
            "USER:",
            "<paste operator details / approval / DO_NOT_DEPLOY / cancel here>",
            "",
            "N8N:",
            "<paste n8n response here>",
            "",
            "FINAL EVIDENCE OR FAILURE MESSAGE",
            "<paste final evidence summary, rejection, failure, or deployment prompt here>",
            "",
            "VALIDATION NOTES",
            "- Parsed command arguments:",
            "- Target KPI vs actual KPI:",
            "- Required tray duration:",
            "- Unwanted box duration:",
            "- Placement warnings:",
            "- Batch gating warnings:",
            "- Priority warnings:",
            "- Deployment offered: <yes/no>",
            "- Deployment response sent: <DO_NOT_DEPLOY/cancel/none>",
            "",
            "COLLECTION COMMAND",
            "python -m tools.m12_collect_manual_result ^",
            f"  --test-id {test_id} ^",
            "  --status <STATUS> ^",
            f"  --chat-transcript outputs/reports/m12/manual_transcripts/{test_id}.txt ^",
            "  --scenario-spec-id <SCN_ID> ^",
            "  --run-id <SIM_ID> ^",
            "  --intent-created-at <UTC_ISO> ^",
            "  --summary-created-at <UTC_ISO>",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", default="outputs/reports/m12/manual_test_packet")
    parser.add_argument("--output", default="outputs/reports/m12/manual_transcripts")
    parser.add_argument("--only", help="Optional test_id to generate one template.")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    packet = Path(args.packet)
    output = Path(args.output)
    if not packet.is_absolute():
        packet = PROJECT_ROOT / packet
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.mkdir(parents=True, exist_ok=True)

    rows = load_manual_rows(packet)
    if args.only:
        rows = [row for row in rows if row.get("test_id") == args.only]
        if not rows:
            raise SystemExit(f"No manual packet row found for test_id={args.only}")
    created = 0
    skipped = 0
    for row in rows:
        test_id = row.get("test_id")
        if not test_id:
            continue
        path = output / f"{test_id}.txt"
        if path.exists() and not args.overwrite:
            skipped += 1
            continue
        path.write_text(template_text(row), encoding="utf-8")
        created += 1
    print(json.dumps({"status": "OK", "created": created, "skipped": skipped, "output": str(output)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
