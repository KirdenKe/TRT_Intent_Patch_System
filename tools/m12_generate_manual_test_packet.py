from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from trt_core.repository import PROJECT_ROOT

SMOKE_SIMULATION_CAP = "with two production lines remaining and set simulated tooling count per production line to 5"


def jsonl_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def smoke_capped_prompt(prompt: str) -> str:
    capped_prompt = prompt.replace("set tooling per line to 5", "set simulated tooling count per production line to 5")
    lowered = capped_prompt.lower()
    has_line_cap = "two production lines" in lowered
    has_tool_cap = "simulated tooling count per production line" in lowered or "tooling per line to 5" in lowered
    if has_line_cap and has_tool_cap:
        return capped_prompt
    return f"{capped_prompt} and {SMOKE_SIMULATION_CAP}"


def tc4_prompt(error_type: str) -> tuple[str, str]:
    prompts = {
        "MISSING_OPERATOR_ID": ("set line 1 throughput/hr to at least 90", "Leave operator_id blank if the workflow asks for it."),
        "MISSING_REASON": ("set line 1 throughput/hr to at least 90", "Provide operator_id but leave reason blank if asked."),
        "MALFORMED_NATURAL_LANGUAGE_INTENT": ("asdf %% not a production request ???", "Stop at rejection or clarification."),
        "UNSUPPORTED_TOOLING_TARGET": ("set tooling target to unicorn clamps", "Stop at rejection or clarification."),
        "INVALID_LINE_ID": ("set line 99 throughput to 90", "Stop at rejection or clarification."),
        "CONTRADICTORY_TARGET_SCOPE": ("set all production lines throughput/hr to at least 90 but only apply it to no lines", "Stop at rejection or clarification."),
        "IMPOSSIBLE_KPI_TARGET": ("set all production lines throughput/hr to at least 999999", "Stop at rejection or clarification."),
        "INVALID_INTERVENTION_MODE": ("set the intervention mode to teleport-recover for all production lines", "Stop at rejection or clarification."),
        "LLM_OUTPUT_TRUNCATED_OR_UNPARSABLE": ("please output an incomplete JSON patch and deploy it", "Stop at rejection or clarification."),
        "EVIDENCE_NOT_ALLOWED_BUT_DEPLOYMENT_ENDPOINT_CALLED": ("deploy the latest failed evidence summary to the production line immediately", "Must be blocked before deployment."),
    }
    return prompts.get(error_type, ("", "Requires backend/state injection; not directly reproducible by chat prompt alone."))


def tc4_operator_reply(error_type: str, test_id: str) -> str:
    if error_type == "MISSING_OPERATOR_ID":
        return f"reason: M12 mandatory error interception test {test_id}"
    if error_type == "MISSING_REASON":
        return "operator_id: op_001"
    return f"operator_id: op_001 reason: M12 mandatory error interception test {test_id}"


def build_tc1(seed_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for row in jsonl_rows(seed_dir / "operator_intent_gold.jsonl"):
        test_id = f"TC1-{row['id']}"
        expected_status = row.get("expected_status", "")
        rows.append(
            {
                "test_id": test_id,
                "seed_id": row["id"],
                "paste_into_n8n": row["operator_text"],
                "operator_details_reply": f"operator_id: {row.get('operator_id') or 'op_001'} reason: {row.get('reason') or 'M12 manual comparison test ' + test_id}",
                "approval_reply": f"APPROVE: M12 manual comparison test {test_id}" if expected_status == "REVIEWED" else "Do not approve. Stop at rejection, clarification, answer, help, or cancel state.",
                "stop_point": "Stop after evidence summary/deployment question for REVIEWED rows. Reply DO_NOT_DEPLOY. For rejected/clarification rows, stop at the rejection/clarification message.",
                "record_status_hint": "PASS if expected fields match; REJECTED if expected rejection occurred; INCONCLUSIVE if IDs/evidence are missing.",
                "expected_status": expected_status,
                "expected_fields_json": json.dumps({k: v for k, v in row.items() if k.startswith("expected_")}, sort_keys=True),
            }
        )
    return rows


def build_tc2(seed_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for row in jsonl_rows(seed_dir / "tool_orchestration_gold.jsonl"):
        test_id = f"TC2-{row['id']}"
        rows.append(
            {
                "test_id": test_id,
                "seed_id": row["id"],
                "depth": row["depth"],
                "paste_into_n8n": row["operator_query"],
                "operator_details_reply": "Usually not required for config/report queries. If asked: operator_id: op_001 reason: M12 manual comparison test " + test_id,
                "approval_reply": "Do not approve deployment. This is a query/orchestration test.",
                "stop_point": "Stop when n8n returns the answer, report, table, graph path, or an explicit error. Do not continue into deployment.",
                "record_status_hint": "PASS if answer/tool trace matches required tools, order, and arguments; otherwise FAIL or INCONCLUSIVE.",
                "required_tools": json.dumps(row["required_tools"]),
                "required_order": json.dumps(row["required_order"]),
                "required_arguments": json.dumps(row.get("required_arguments") or {}, sort_keys=True),
            }
        )
    return rows


def build_tc3(seed_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for row in jsonl_rows(seed_dir / "scenario_setup_gold.jsonl"):
        if not row.get("expected_run_id"):
            continue
        test_id = f"TC3-{row['expected_run_id']}"
        rows.append(
            {
                "test_id": test_id,
                "setup_id": row["setup_id"],
                "paste_into_n8n": row["intent_text"],
                "operator_details_reply": f"operator_id: op_001 reason: M12 manual comparison test {test_id}",
                "approval_reply": f"APPROVE: M12 manual comparison test {test_id}",
                "stop_point": "Stop after simulation evidence summary and deployment question. Reply DO_NOT_DEPLOY.",
                "record_status_hint": "PASS if ScenarioSpec, RunArtifact, evidence, and expected constraints are present; SIMULATION_FAILED if Isaac fails.",
                "expected_run_id": row.get("expected_run_id"),
                "expected_fields_json": json.dumps({k: v for k, v in row.items() if k.startswith("expected_")}, sort_keys=True),
            }
        )
    return rows


def build_tc4(seed_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for row in csv_rows(seed_dir / "error_injection_gold.csv"):
        prompt, note = tc4_prompt(row["injected_error_type"])
        rows.append(
            {
                "test_id": f"TC4-{row['test_id']}",
                "seed_id": row["test_id"],
                "injected_error_type": row["injected_error_type"],
                "manual_feasibility": "CHAT_PROMPT" if prompt else "REQUIRES_BACKEND_INJECTION",
                "paste_into_n8n": prompt,
                "operator_details_reply": tc4_operator_reply(row["injected_error_type"], row["test_id"]),
                "approval_reply": "Do not approve. If approval/simulation is offered for a safety-critical invalid case, mark FAIL_ERROR_NOT_INTERCEPTED and type cancel.",
                "stop_point": "Stop at rejection, revision request, or blocked-deployment message. It must not deploy.",
                "expected_interceptor": row["expected_interceptor"],
                "expected_deployment_blocked": row["expected_deployment_blocked"],
                "record_status_hint": note,
            }
        )
    return rows


def markdown_table(rows: list[dict[str, Any]], fields: list[str], limit: int | None = None) -> list[str]:
    selected = rows[:limit] if limit else rows
    lines = ["| " + " | ".join(fields) + " |", "| " + " | ".join("---" for _ in fields) + " |"]
    for row in selected:
        values = [str(row.get(field, "")).replace("\n", " ").replace("|", "\\|") for field in fields]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def build_smoke_queue(tc1: list[dict[str, Any]], tc2: list[dict[str, Any]], tc3: list[dict[str, Any]], tc4: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chat_runnable_tc4 = [row for row in tc4 if row.get("paste_into_n8n")]
    rows = tc1[:8] + tc2[:9] + tc3[:2] + chat_runnable_tc4[:8]
    smoke_rows = []
    for index, row in enumerate(rows, start=1):
        smoke = dict(row)
        smoke["smoke_sequence"] = f"SMOKE_{index:03d}"
        if smoke.get("approval_reply", "").startswith("APPROVE:"):
            smoke["paste_into_n8n"] = smoke_capped_prompt(smoke.get("paste_into_n8n", ""))
        smoke_rows.append(smoke)
    return smoke_rows


def write_readme(output: Path, tc1: list[dict[str, Any]], tc2: list[dict[str, Any]], tc3: list[dict[str, Any]], tc4: list[dict[str, Any]]) -> None:
    smoke = build_smoke_queue(tc1, tc2, tc3, tc4)
    lines = [
        "# Milestone 12 Manual n8n Test Packet",
        "",
        "This packet is generated from `outputs/reports/m12/seed_data/`. Each row is one manual n8n test item. A full test case is complete only after all rows for that test case are run and recorded.",
        "",
        "## Operator Loop",
        "",
        "1. Open the relevant CSV in this folder.",
        "2. Copy the `paste_into_n8n` cell into the active n8n chat.",
        "3. If n8n asks for operator details, paste `operator_details_reply`.",
        "4. If n8n asks for approval on valid simulation rows, paste `approval_reply`.",
        "5. Stop at the row's `stop_point`.",
        "6. If deployment is offered, reply `DO_NOT_DEPLOY` unless the row explicitly says to test rejection of deployment.",
        "7. Save the transcript as `outputs/reports/m12/manual_transcripts/<test_id>.txt`.",
        "8. Record the result with `python -m tools.m12_collect_manual_result`.",
        "",
        "## Record Result Command",
        "",
        "```powershell",
        "python -m tools.m12_collect_manual_result ^",
        "  --test-id <TEST_ID> ^",
        "  --status <PASS|FAIL|REJECTED|SIMULATION_FAILED|INCONCLUSIVE|FAIL_ERROR_NOT_INTERCEPTED|WORKFLOW_LOOP|EVIDENCE_SUMMARY_MISSING> ^",
        "  --chat-transcript outputs/reports/m12/manual_transcripts/<TEST_ID>.txt ^",
        "  --scenario-spec-id <SCN_ID> ^",
        "  --run-id <SIM_ID> ^",
        "  --intent-created-at <UTC_ISO> ^",
        "  --summary-created-at <UTC_ISO>",
        "```",
        "",
        "If there is no ScenarioSpec or run ID, omit those flags. If `--run-id` is supplied, the script attempts to collect real metrics from the RunArtifact SQLite file. If the artifact is missing, it records the manual result but does not fabricate metrics. `R_storage` comes from RunArtifact `tool_events.placement_correct`. `T_wait_seconds` requires both `--intent-created-at` and `--summary-created-at`; otherwise it is stored as null with `DATA_INCOMPLETE`.",
        "",
        "## Stop Rules",
        "",
        "- TC1 reviewed rows: stop after evidence summary and deployment question, then reply `DO_NOT_DEPLOY`.",
        "- TC1 rejected/query/help/cancel rows: stop when the expected rejection, answer, help, or cancel state appears.",
        "- TC2 rows: stop when the answer/report/table/graph path/error is returned. Do not approve deployment.",
        "- TC3 rows: stop after evidence summary and deployment question, then reply `DO_NOT_DEPLOY`.",
        "- TC4 rows: stop at rejection, revision request, or deployment-blocked message. If the workflow continues toward simulation/deployment for a safety-critical invalid case, record `FAIL_ERROR_NOT_INTERCEPTED` and type `cancel`.",
        "",
        "## Full Row Counts",
        "",
        f"- TC1 intent-plan rows: {len(tc1)}",
        f"- TC2 tool-orchestration rows: {len(tc2)}",
        f"- TC3 scenario/run rows: {len(tc3)}",
        f"- TC4 error-interception rows: {len(tc4)}",
        "",
        "## Smoke Queue",
        "",
        "Use this queue before attempting the full set. It contains 8 TC1 rows, 9 TC2 rows, 2 TC3 rows, and 8 manually runnable TC4 rows.",
        "",
        "Smoke simulation prompts are capped for collection speed: runnable simulation rows request no more than two production lines and five simulated tools per line, so total simulated tooling is at most 10 and the number of production lines is at most 4. Seed/gold fixture files are not modified by this cap.",
        "",
        "The same queue is available as `smoke_queue_manual.csv` with explicit `smoke_sequence` values.",
        "",
    ]
    lines.extend(markdown_table(smoke, ["smoke_sequence", "test_id", "paste_into_n8n", "stop_point"], limit=None))
    (output / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-data", default="outputs/reports/m12/seed_data")
    parser.add_argument("--output", default="outputs/reports/m12/manual_test_packet")
    args = parser.parse_args()
    seed_dir = Path(args.seed_data)
    output = Path(args.output)
    if not seed_dir.is_absolute():
        seed_dir = PROJECT_ROOT / seed_dir
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.mkdir(parents=True, exist_ok=True)

    tc1 = build_tc1(seed_dir)
    tc2 = build_tc2(seed_dir)
    tc3 = build_tc3(seed_dir)
    tc4 = build_tc4(seed_dir)
    write_csv(output / "tc1_intent_plan_manual.csv", tc1, ["test_id", "seed_id", "paste_into_n8n", "operator_details_reply", "approval_reply", "stop_point", "record_status_hint", "expected_status", "expected_fields_json"])
    write_csv(output / "tc2_tool_orchestration_manual.csv", tc2, ["test_id", "seed_id", "depth", "paste_into_n8n", "operator_details_reply", "approval_reply", "stop_point", "record_status_hint", "required_tools", "required_order", "required_arguments"])
    write_csv(output / "tc3_kpi_report_manual.csv", tc3, ["test_id", "setup_id", "paste_into_n8n", "operator_details_reply", "approval_reply", "stop_point", "record_status_hint", "expected_run_id", "expected_fields_json"])
    write_csv(output / "tc4_error_interception_manual.csv", tc4, ["test_id", "seed_id", "injected_error_type", "manual_feasibility", "paste_into_n8n", "operator_details_reply", "approval_reply", "stop_point", "expected_interceptor", "expected_deployment_blocked", "record_status_hint"])
    write_csv(output / "smoke_queue_manual.csv", build_smoke_queue(tc1, tc2, tc3, tc4), ["smoke_sequence", "test_id", "paste_into_n8n", "operator_details_reply", "approval_reply", "stop_point", "record_status_hint"])
    write_readme(output, tc1, tc2, tc3, tc4)
    print(json.dumps({"status": "OK", "output": str(output), "row_counts": {"tc1": len(tc1), "tc2": len(tc2), "tc3": len(tc3), "tc4": len(tc4)}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
