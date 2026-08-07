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
        "INVALID_LINE_ID": ("set production line -2 throughput/hr to at least 90", "Negative production-line identifiers are invalid; large positive line counts are scalability cases."),
        "CONTRADICTORY_TARGET_SCOPE": ("set all production lines throughput/hr to at least 90 but only apply it to no lines", "Stop at rejection or clarification."),
        "IMPOSSIBLE_KPI_TARGET": ("set all production lines throughput/hr to at least -2", "Negative throughput is invalid; large positive targets require feasibility evaluation."),
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
        override = _smoke_override(smoke.get("test_id", ""))
        smoke.update(override)
        smoke_rows.append(smoke)
    return smoke_rows


def _smoke_override(test_id: str) -> dict[str, Any]:
    simulations = {
        "TC1-INTENT_001": (
            "set line 1 throughput/hr to at least 90 and simulate four production lines with 2 tooling per line",
            {"expected_kpi_updates": {"min_throughput_per_hour": 90}, "expected_target_lines": ["line_1"], "expected_simulation_config_updates": {"num_envs": 4, "add_reference_number": 2}, "expected_status": "REVIEWED"},
        ),
        "TC1-INTENT_002": (
            "set all production lines throughput/hr to at least 120 and simulate four production lines with 2 tooling per line",
            {"expected_target_scope": "ALL_LINES", "expected_kpi_updates": {"min_throughput_per_hour": 120}, "expected_simulation_config_updates": {"num_envs": 4, "add_reference_number": 2}, "expected_status": "REVIEWED"},
        ),
        "TC1-INTENT_003": (
            "with two production lines remaining, stop robotic arms immediately upon anomaly detection and set simulated tooling count per production line to 5",
            {"expected_simulation_config_updates": {"num_envs": 2, "chosen_intervention_mode": "immediate-stop", "add_reference_number": 5}, "expected_status": "REVIEWED"},
        ),
        "TC1-INTENT_004": (
            "with two production lines remaining, reduce the current arrival time by 0.5 seconds, reduce the current entanglement fix time by 1 second, make the current recovery delay 1 second slower, and simulate 4 tooling per line",
            {"expected_simulation_config_updates": {"travel_time": 0.5, "fix_duration": 2.0, "resume_delay": 2.0, "num_envs": 2, "add_reference_number": 4}, "expected_status": "REVIEWED"},
        ),
        "TC1-INTENT_005": (
            "set production line 2 tooling picking target to knife handle and simulate four production lines with 2 tooling per line",
            {"expected_target_lines": ["line_2"], "expected_tooling_policy": {"selected_normalized_types": ["KNIFE_HANDLE"]}, "expected_simulation_config_updates": {"num_envs": 4, "add_reference_number": 2}, "expected_status": "REVIEWED"},
        ),
        "TC1-INTENT_006": (
            "set line 1 picking order to prioritize tooling other than scissors and simulate four production lines with 2 tooling per line",
            {"expected_target_lines": ["line_1"], "expected_manipulator_priority": {"excluded_normalized_types": ["SCISSORS"]}, "expected_simulation_config_updates": {"num_envs": 4, "add_reference_number": 2}, "expected_status": "REVIEWED"},
        ),
        "TC3-fixture_our_setup_i_01": (
            "set all production lines throughput/hr to at least 90 and keep placement verification strict; simulate four production lines with 2 tooling per line",
            {"expected_kpi_updates": {"min_throughput_per_hour": 90}, "expected_simulation_config_updates": {"num_envs": 4, "add_reference_number": 2}, "expected_constraints": ["placement_verification_required"], "expected_status": "REVIEWED"},
        ),
        "TC3-fixture_our_setup_i_02": (
            "set all production lines throughput/hr to at least 90 and keep placement verification strict; simulate four production lines with 2 tooling per line",
            {"expected_kpi_updates": {"min_throughput_per_hour": 90}, "expected_simulation_config_updates": {"num_envs": 4, "add_reference_number": 2}, "expected_constraints": ["placement_verification_required"], "expected_status": "REVIEWED"},
        ),
    }
    if test_id in simulations:
        prompt, expected = simulations[test_id]
        return {"paste_into_n8n": prompt, "expected_status": expected["expected_status"], "expected_fields_json": json.dumps(expected, sort_keys=True)}
    if test_id == "TC1-INTENT_007":
        expected = {
            "expected_status": "NEEDS_CLARIFICATION",
            "expected_error_type": "PRODUCTION_LINE_DEFINITIONS_REQUIRED",
            "expected_requested_line_count": 99,
        }
        return {
            "paste_into_n8n": "generate a task requirement table for 99 production lines with minimum throughput/hr 90",
            "expected_status": "NEEDS_CLARIFICATION",
            "expected_fields_json": json.dumps(expected, sort_keys=True),
            "record_status_hint": "PASS if the system treats 99 lines as a valid scalability request and asks for missing line/workstation definitions or produces a correctly scoped 99-line table; it must not reject the number merely for being large.",
        }
    if test_id == "TC2-TOOL_L1_002":
        return {
            "paste_into_n8n": "calculate reset completion rate for the latest run",
            "required_arguments": json.dumps({"run_selector": "latest"}, sort_keys=True),
            "record_status_hint": "PASS only if R_reset is computed from a real latest RunArtifact; sim_seed_001 is not used in live smoke testing.",
        }
    if test_id == "TC2-TOOL_L1_006":
        return {
            "paste_into_n8n": "show task_table for line_2",
            "required_arguments": json.dumps({"line_ids": ["line_2"], "limit": None}, sort_keys=True),
            "record_status_hint": "PASS only if the line_2 task table is loaded from the current TRT; this preserves the original row count without duplicating the line_1 query.",
        }
    return {}


def build_smoke_extensions(smoke: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source = smoke[0]
    return [
        {
            "extension_id": "SMOKE_028",
            "test_case_id": "TC5",
            "execution_mode": "DERIVED_LIVE_LIFECYCLE",
            "source_smoke_sequence": source["smoke_sequence"],
            "natural_language_trigger": source["paste_into_n8n"],
            "repetitions": 1,
            "models": "n8n + trt-api + Isaac Sim",
            "pass_criteria": "The source live run records intent, summary, ScenarioSpec, startup boundary, RunArtifact, and review timestamps; T_verification excludes measured Isaac startup.",
        },
        {
            "extension_id": "SMOKE_029",
            "test_case_id": "TC6",
            "execution_mode": "DIRECT_LLM_REPEAT",
            "source_smoke_sequence": "",
            "natural_language_trigger": source["paste_into_n8n"],
            "repetitions": 3,
            "models": "cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit",
            "pass_criteria": "Three identical prompt executions are scored for JSON validity, required fields, classification, semantics, variation, latency, and tokens.",
        },
        {
            "extension_id": "SMOKE_030",
            "test_case_id": "TC7",
            "execution_mode": "DIRECT_LLM_MODEL_COMPARISON",
            "source_smoke_sequence": "",
            "natural_language_trigger": source["paste_into_n8n"],
            "repetitions": 3,
            "models": "Gemma; Qwen; Llama",
            "pass_criteria": "The same prompt and structured schema are run three times against each model without client-side sampling overrides.",
        },
    ]


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
        "Use this 27-case core queue before attempting the full set. It contains 8 TC1 rows, 9 TC2 rows, 2 TC3 rows, and 8 manually runnable TC4 rows. These counts preserve the prior literature-comparison denominator.",
        "",
        "Smoke simulation prompts are capped for collection speed: every runnable simulation row uses at most four production lines and at most ten total tools across those lines. The queue uses 4 x 2, 2 x 4, or 2 x 5 configurations. Seed/gold fixture files are not modified by this cap.",
        "",
        "The same queue is available as `smoke_queue_manual.csv` with explicit `smoke_sequence` values.",
        "",
    ]
    lines.extend(markdown_table(smoke, ["smoke_sequence", "test_id", "paste_into_n8n", "stop_point"], limit=None))
    lines.extend([
        "",
        "## TC5-TC7 Smoke Extensions",
        "",
        "The complete smoke suite contains 30 checks. SMOKE_028 (TC5) reuses the lifecycle of the first successful live core case; it does not launch an extra simulation. SMOKE_029 (TC6) and SMOKE_030 (TC7) run one fixture three times against Gemma, Qwen, and Llama. They are reported separately and are not added to the 27-case TC1-TC4 literature denominator.",
        "",
    ])
    lines.extend(markdown_table(build_smoke_extensions(smoke), ["extension_id", "test_case_id", "execution_mode", "repetitions", "models", "pass_criteria"], limit=None))
    lines.extend([
        "",
        "Validate the extension plan without calling n8n, Isaac, or any model endpoint:",
        "",
        "```powershell",
        "python -m tools.m12_run_smoke_extensions --plan-only --output outputs/reports/m12/smoke_extensions",
        "```",
        "",
        "After the 27-case n8n run and metric collection finish, execute TC5-TC7 with:",
        "",
        "```powershell",
        "python -m tools.m12_run_smoke_extensions --n8n-results <RUN_DIR>/full_n8n_results_latest.csv --metrics-db outputs/reports/m12/m12_metrics.sqlite3 --output <RUN_DIR>/smoke_extensions",
        "```",
        "",
        "The smoke suite is not complete until all 30 checks pass this gate:",
        "",
        "```powershell",
        "python -m tools.m12_validate_smoke_suite --core-results <RUN_DIR>/full_n8n_results_latest.csv --extension-results <RUN_DIR>/smoke_extensions/smoke_extension_results.csv --output <RUN_DIR>/smoke_suite_status.json",
        "```",
    ])
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
    smoke = build_smoke_queue(tc1, tc2, tc3, tc4)
    write_csv(output / "smoke_queue_manual.csv", smoke, ["smoke_sequence", "test_id", "paste_into_n8n", "operator_details_reply", "approval_reply", "stop_point", "record_status_hint", "expected_status", "expected_fields_json", "required_tools", "required_order", "required_arguments", "expected_interceptor", "expected_deployment_blocked"])
    write_csv(output / "smoke_extension_tc5_tc7.csv", build_smoke_extensions(smoke), ["extension_id", "test_case_id", "execution_mode", "source_smoke_sequence", "natural_language_trigger", "repetitions", "models", "pass_criteria"])
    write_readme(output, tc1, tc2, tc3, tc4)
    print(json.dumps({"status": "OK", "output": str(output), "row_counts": {"tc1": len(tc1), "tc2": len(tc2), "tc3": len(tc3), "tc4": len(tc4)}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
