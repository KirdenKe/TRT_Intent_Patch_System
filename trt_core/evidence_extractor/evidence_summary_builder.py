"""Build deterministic RunArtifact evidence summaries for operator deployment decisions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from trt_core.digital_twin_adapter.result_reader import read_simulation_results
from trt_core.repository import TRTRepository


DEFAULT_SEED_SWEEP_DB_PATH = (
    r"C:\Dev\IsaacSim\_build\windows-x86_64\release\standalone_examples\api"
    r"\isaacsim.robot.manipulators\ur5\tasks\seed_sweep.sqlite3"
)

OPERATOR_CHECK_TRANSLATIONS: dict[str, dict[str, str]] = {
    "batch_gating.table_not_empty_before_next_batch": {
        "operator_label": "Table was not cleared before the next batch arrived",
        "operator_explanation": (
            "The robot brought in or requested the next group of tools before all tools already on the table were picked. "
            "This can crowd the table and increase the chance of failed grasps."
        ),
        "operator_impact": "Do not deploy yet. The robot may stack or crowd tools on the line.",
        "recommended_action": "Revise the strategy so each table batch is fully cleared before the next batch is requested.",
    },
    "priority.required_first": {
        "operator_label": "ENT-required tools were not picked first",
        "operator_explanation": "The robot picked non-ENT tooling before finishing the ENT-required tools available on the table.",
        "operator_impact": "Do not deploy yet. The robot is not following the requested ENT-first picking rule.",
        "recommended_action": "Revise or rerun after fixing the robot picking order policy.",
    },
    "throughput.min_per_hour": {
        "operator_label": "Throughput target was missed",
        "operator_explanation": "The simulated line processed fewer tools per hour than the minimum target.",
        "operator_impact": "Deployment may reduce line productivity.",
        "recommended_action": "Revise the strategy or rerun with adjusted settings.",
    },
    "result_data.missing_priority_events": {
        "operator_label": "Missing priority evidence",
        "operator_explanation": "The simulation completed, but the result database did not contain the records needed to verify picking order.",
        "operator_impact": "The deployment decision cannot be trusted yet.",
        "recommended_action": "Rerun simulation with priority event logging enabled.",
    },
    "simulation_scope.line_kpis_missing": {
        "operator_label": "Missing line KPI evidence",
        "operator_explanation": "The simulation did not record KPI evidence for a production line that was included in the simulation scope.",
        "operator_impact": "The deployment decision cannot be trusted yet.",
        "recommended_action": "Rerun simulation and verify KPI rows are written for every simulated line.",
    },
    "result_data.missing_kpi_rows": {
        "operator_label": "Missing KPI evidence",
        "operator_explanation": "The simulation completed, but the result database did not contain line KPI rows.",
        "operator_impact": "The deployment decision cannot be trusted yet.",
        "recommended_action": "Rerun simulation after fixing result recording.",
    },
    "downtime.max_seconds": {
        "operator_label": "Downtime limit was exceeded",
        "operator_explanation": "The simulated line had more downtime than the allowed limit.",
        "operator_impact": "Deployment may reduce line availability.",
        "recommended_action": "Revise the strategy to reduce downtime or adjust the validated limit.",
    },
    "kpi_scope.full_environment_expected_for_limited_simulation": {
        "operator_label": "KPI scope expected the full environment",
        "operator_explanation": (
            "The simulation was limited to a smaller number of tools, but the KPI checker still expected the full "
            "production-line inventory to be sorted."
        ),
        "operator_impact": "Do not deploy yet. The KPI result is measuring a full-inventory condition against a partial-table simulation.",
        "recommended_action": "Update the KPI checker to evaluate the tools actually shown in the simulation, then rerun.",
    },
    "placement.incorrect_required_tool": {
        "operator_label": "Required tool placement warning",
        "operator_explanation": "A required ENT tool was picked and sent to the required tray, but the result database marked its placement as incorrect.",
        "operator_impact": "Review before deployment. The robot behavior may be correct, but placement validation disagreed.",
        "recommended_action": "Inspect the placement validator and rerun the simulation.",
    },
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_path(repository: TRTRepository, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return repository.root / path


def _load_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _find_scenario_spec_path(repository: TRTRepository, scenario_spec_id: str | None) -> Path | None:
    if not scenario_spec_id:
        return None
    matches = sorted((repository.root / "outputs" / "scenario_specs").glob(f"{scenario_spec_id}.json"))
    return matches[-1] if matches else None


def _output_db_path(repository: TRTRepository, run_id: str, explicit: str | None = None) -> Path:
    if explicit:
        resolved = _resolve_path(repository, explicit)
        if resolved:
            return resolved
    return repository.root / "outputs" / "run_artifacts" / f"{run_id}.sqlite"


def _latest_seed_sweep_failure(seed_db_path: str | None) -> dict[str, Any] | None:
    if not seed_db_path:
        return None
    path = Path(seed_db_path)
    if not path.exists():
        return None
    try:
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT id, created_at, status, failure_stage, failure_reason, failure_details_json
                  FROM sweep_runs
              ORDER BY created_at DESC
                 LIMIT 1
                """
            ).fetchone()
            return None if row is None else dict(row)
    except sqlite3.Error:
        return None


def _line_target(line: dict[str, Any], key: str) -> float | None:
    kpi = line.get("kpi") or {}
    value = kpi.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _policy_for_line(scenario_spec: dict[str, Any], line_id: str) -> dict[str, Any]:
    for policy in scenario_spec.get("line_policies") or []:
        if policy.get("line_id") == line_id:
            return policy
    return {}


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_number(value: float | int | None, suffix: str = "") -> str:
    if value is None:
        return "not recorded"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value)}{suffix}"
    return f"{value:g}{suffix}" if isinstance(value, float) else f"{value}{suffix}"


def _failed_check(
    *,
    line_id: str,
    check_id: str,
    expected: str,
    actual: str,
    actual_value: Any,
    expected_value: Any,
    evidence_source: str,
    operator_explanation: str,
    severity: str = "FAIL",
    evidence_row_id: Any = None,
    deployment_blocking: bool = True,
) -> dict[str, Any]:
    translation = OPERATOR_CHECK_TRANSLATIONS.get(check_id, {})
    return {
        "line_id": line_id,
        "check_id": check_id,
        "technical_check_id": check_id,
        "operator_label": translation.get("operator_label") or check_id.replace("_", " ").replace(".", ": "),
        "operator_explanation": translation.get("operator_explanation") or operator_explanation,
        "operator_impact": translation.get("operator_impact") or ("Deployment is not recommended." if deployment_blocking else "Review before deployment."),
        "recommended_action": translation.get("recommended_action") or "Revise the request or rerun the simulation.",
        "severity": severity,
        "expected": expected,
        "actual": actual,
        "actual_value": actual_value,
        "expected_value": expected_value,
        "evidence_source": evidence_source,
        "evidence_row_id": evidence_row_id,
        "deployment_blocking": deployment_blocking,
        "technical_explanation": operator_explanation,
    }


def _priority_events_for_line(priority_events: list[dict[str, Any]], line_id: str) -> list[dict[str, Any]]:
    return [event for event in priority_events if str(event.get("line_id") or "") == line_id]


def _priority_event_row_id(event: dict[str, Any]) -> Any:
    return (
        event.get("rowid")
        or event.get("id")
        or event.get("event_id")
        or event.get("actual_pick_index")
        or event.get("actual_pick_index_in_line")
    )


def _priority_event_sort_key(event: dict[str, Any]) -> tuple[int, int, int]:
    return (
        _int_or_zero(event.get("batch_index")),
        _int_or_zero(event.get("actual_pick_index_in_batch") or event.get("actual_pick_index") or event.get("actual_pick_index_in_line")),
        _int_or_zero(event.get("tool_number")),
    )


def _priority_event_batch_key(event: dict[str, Any]) -> Any:
    return event.get("batch_id") if event.get("batch_id") is not None else _int_or_zero(event.get("batch_index"))


def _evaluate_required_first_priority_events(line_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the first unwanted pick that occurs while same-batch wanted tools remain."""
    if not line_events:
        return {
            "status": "NO_DATA",
            "reason": "No priority event rows were recorded for this line.",
            "deviating_event": None,
        }
    batches: dict[Any, list[dict[str, Any]]] = {}
    for event in line_events:
        batches.setdefault(_priority_event_batch_key(event), []).append(event)

    for batch_events in batches.values():
        ordered_events = sorted(batch_events, key=_priority_event_sort_key)
        wanted_remaining = {
            str(event.get("tool_id") or event.get("tool_number"))
            for event in ordered_events
            if _int_or_zero(event.get("wanted")) == 1
        }
        for event in ordered_events:
            tool_ref = str(event.get("tool_id") or event.get("tool_number"))
            if _int_or_zero(event.get("wanted")) == 1:
                wanted_remaining.discard(tool_ref)
                continue
            if wanted_remaining:
                return {
                    "status": "FAIL",
                    "reason": "An unwanted tool was picked while wanted ENT tools from the same table batch were still unpicked.",
                    "deviating_event": event,
                }
    return {
        "status": "PASS",
        "reason": "All wanted table tools were picked before unwanted table tools in each evaluated batch.",
        "deviating_event": None,
    }


def _priority_failure_checks(
    *,
    line_id: str,
    priority_policy: str,
    priority_enabled: bool,
    priority_deviation_count: int,
    priority_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not priority_enabled:
        return checks
    if priority_policy in {"", "NONE"}:
        checks.append(
            _failed_check(
                line_id=line_id,
                check_id="priority.policy_missing",
                expected="A manipulator priority policy should be recorded when priority validation is enabled.",
                actual="No priority policy was recorded for this line.",
                actual_value=priority_policy or "NONE",
                expected_value="FCFS or configured manipulator priority policy",
                evidence_source="line_kpis.priority_policy",
                operator_explanation=f"{line_id} failed because the simulation did not record the active manipulator priority policy.",
            )
        )
        return checks

    line_events = _priority_events_for_line(priority_events, line_id)
    if priority_policy == "REQUIRED_FIRST":
        evaluation = _evaluate_required_first_priority_events(line_events)
        deviating_event = evaluation.get("deviating_event")
        if deviating_event:
            tool_value = deviating_event.get("tool_id") or deviating_event.get("tool_number") or "unknown"
            reason = str(evaluation.get("reason") or "The robot picked unwanted tooling before completing required tooling.")
            checks.append(
                _failed_check(
                    line_id=line_id,
                    check_id="priority.required_first",
                    expected="Robot must pick ENT-required tools before unwanted tools when required tools are available on the table.",
                    actual=reason,
                    actual_value=tool_value,
                    expected_value="ENT-required tooling first",
                    evidence_source="priority_events",
                    evidence_row_id=_priority_event_row_id(deviating_event),
                    operator_explanation=f"{line_id} failed because the REQUIRED_FIRST manipulator priority rule was violated.",
                )
            )
        elif priority_deviation_count > 0 and not line_events:
            checks.append(
                _failed_check(
                    line_id=line_id,
                    check_id="priority.required_first",
                    expected="Robot must pick ENT-required tools before unwanted tools when required tools are available on the table.",
                    actual=f"{priority_deviation_count} priority deviation(s) were recorded.",
                    actual_value=priority_deviation_count,
                    expected_value=0,
                    evidence_source="line_kpis.priority_deviation_count",
                    operator_explanation=f"{line_id} failed because REQUIRED_FIRST recorded {priority_deviation_count} priority deviation(s).",
                )
            )
        elif not line_events:
            checks.append(
                _failed_check(
                    line_id=line_id,
                    check_id="result_data.missing_priority_events",
                    expected="priority_events rows for REQUIRED_FIRST validation",
                    actual="No priority_events rows were recorded for this line.",
                    actual_value=0,
                    expected_value="at least one priority event row",
                    evidence_source="output_db.priority_events",
                    operator_explanation=f"{line_id} failed because REQUIRED_FIRST could not be validated without priority event evidence.",
                )
            )
    return checks


def _batch_gating_row_is_real_violation(row: dict[str, Any]) -> bool:
    if _int_or_zero(row.get("batch_gating_violation")) == 0:
        return False
    table_tool_count = _int_or_zero(row.get("table_tool_count"))
    picked_count = _int_or_zero(row.get("picked_count"))
    completed_at = _float_or_none(row.get("batch_completed_at_seconds"))
    next_requested_at = _float_or_none(row.get("next_batch_requested_at_seconds"))
    success = _int_or_zero(row.get("success")) == 1

    batch_was_cleared = table_tool_count > 0 and picked_count >= table_tool_count
    # Some Isaac rows record a blocked early batch attempt as a gating flag, then
    # later clear the table before the actual next batch is loaded. Under the
    # operator-facing KPI rule, that is not "the next batch arrived early".
    if success and batch_was_cleared:
        return False
    next_request_after_completion = completed_at is not None and (
        next_requested_at is None or next_requested_at >= completed_at
    )
    if success and batch_was_cleared and next_request_after_completion:
        return False
    if batch_was_cleared and next_request_after_completion:
        return False
    return True


def _batch_gating_checks(line_id: str, batch_completion_kpis: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for row in batch_completion_kpis:
        if str(row.get("line_id") or "") != line_id or not _batch_gating_row_is_real_violation(row):
            continue
        batch_id = row.get("batch_id") or row.get("batch_index") or "unknown batch"
        checks.append(
            _failed_check(
                line_id=line_id,
                check_id="batch_gating.table_not_empty_before_next_batch",
                expected="The next table batch must not be requested until all current table tools are picked.",
                actual=f"Batch gating violation recorded for {batch_id}.",
                actual_value=batch_id,
                expected_value="no batch gating violations",
                evidence_source="batch_completion_kpis.batch_gating_violation",
                evidence_row_id=row.get("id") or row.get("batch_index"),
                operator_explanation=f"{line_id} failed because a next batch was requested before the current table was cleared.",
            )
        )
    return checks


def _rows_for_line(rows: list[dict[str, Any]], line_id: str) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get("line_id") or "") == line_id]


def _container_row(container_completion_events: list[dict[str, Any]], line_id: str, container_type: str) -> dict[str, Any] | None:
    for row in container_completion_events:
        if str(row.get("line_id") or "") == line_id and str(row.get("container_type") or "") == container_type:
            return row
    return None


def _limited_simulation_scope_check(
    *,
    line_id: str,
    line_kpi: dict[str, Any],
    scenario_spec: dict[str, Any],
    container_completion_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    simulation_config = scenario_spec.get("simulation_config") or {}
    simulated_tooling_count = _int_or_zero(simulation_config.get("add_reference_number"))
    if simulated_tooling_count <= 0:
        return None
    completed_count = _int_or_zero(line_kpi.get("completed_count"))
    all_sorting = _container_row(container_completion_events, line_id, "ALL_SORTING")
    if not all_sorting:
        return None
    expected_full_count = _int_or_zero(all_sorting.get("required_count"))
    completed_all_sorting = _int_or_zero(all_sorting.get("completed_count"))
    if (
        expected_full_count > simulated_tooling_count
        and completed_count == simulated_tooling_count
        and completed_all_sorting == completed_count
        and _int_or_zero(all_sorting.get("success")) == 0
    ):
        check = _failed_check(
            line_id=line_id,
            check_id="kpi_scope.full_environment_expected_for_limited_simulation",
            expected=f"Evaluate the {simulated_tooling_count} tools shown in this simulation run.",
            actual=(
                f"The KPI checker expected {expected_full_count} full-environment tools, "
                f"while the run completed {completed_count} shown tools."
            ),
            actual_value=completed_count,
            expected_value=simulated_tooling_count,
            evidence_source="container_completion_events.ALL_SORTING",
            evidence_row_id=all_sorting.get("id") or all_sorting.get("rowid"),
            operator_explanation=(
                f"{line_id} sorted all {completed_count} tools shown in the simulation, but the KPI checker "
                f"expected the full {expected_full_count}-tool environment."
            ),
            severity="OPERATOR_ACK_REQUIRED",
            deployment_blocking=False,
        )
        check["simulated_tooling_count"] = simulated_tooling_count
        check["full_environment_tooling_count"] = expected_full_count
        check["kpi_evaluation_scope"] = "SIMULATED_TABLE_SCOPE"
        return check
    return None


def _placement_warnings(line_id: str, tool_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for event in _rows_for_line(tool_events, line_id):
        if _int_or_zero(event.get("picked")) != 1 or _int_or_zero(event.get("placed")) != 1:
            continue
        if _int_or_zero(event.get("placement_correct")) == 1:
            continue
        tool_id = str(event.get("tool_id") or event.get("tool_number") or "unknown tool")
        target = str(event.get("placement_target") or "unknown target")
        wanted = _int_or_zero(event.get("wanted")) == 1
        warning = _failed_check(
            line_id=line_id,
            check_id="placement.incorrect_required_tool" if wanted else "placement.incorrect_tool",
            expected="Picked tools should be marked correctly placed in their target container.",
            actual=f"{tool_id} was sent to {target}, but placement_correct was 0.",
            actual_value=tool_id,
            expected_value="placement_correct=1",
            evidence_source="tool_events.placement_correct",
            evidence_row_id=event.get("id") or event.get("rowid") or event.get("actual_pick_index"),
            operator_explanation=(
                f"{line_id} warning: {tool_id} was sent to {target}, but the result database marked it as not correctly placed."
            ),
            severity="WARNING",
            deployment_blocking=False,
        )
        warning["operator_explanation"] = (
            f"{line_id} warning: {tool_id} was sent to {target}, but the result database marked it as not correctly placed."
        )
        warnings.append(warning)
    return warnings


def _recovered_batch_gating_warnings(line_id: str, batch_completion_kpis: list[dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for row in _rows_for_line(batch_completion_kpis, line_id):
        if _int_or_zero(row.get("batch_gating_violation")) == 0:
            continue
        if _batch_gating_row_is_real_violation(row):
            continue
        table_tool_count = _int_or_zero(row.get("table_tool_count"))
        picked_count = _int_or_zero(row.get("picked_count"))
        batch_id = row.get("batch_id") or row.get("batch_index") or "unknown batch"
        warnings.append(
            {
                "line_id": line_id,
                "check_id": "batch_gating.recovered_blocked_next_batch_request",
                "technical_check_id": "batch_gating.recovered_blocked_next_batch_request",
                "operator_label": "Next-batch request was blocked until the table cleared",
                "operator_explanation": (
                    f"{line_id} warning: a next-batch request was blocked for {batch_id}, then the table was cleared "
                    f"({picked_count}/{table_tool_count} tools picked) before the next group was loaded."
                ),
                "operator_impact": "This is a recovered warning, not a table-crowding failure.",
                "recommended_action": "Keep the batch gate enabled and review why the early request was attempted.",
                "severity": "WARNING",
                "expected": "No next-batch request should be attempted while table tools remain.",
                "actual": "An early request was recorded but blocked/recovered before the next batch loaded.",
                "actual_value": batch_id,
                "expected_value": "request only after table clear",
                "evidence_source": "batch_completion_kpis.batch_gating_violation",
                "evidence_row_id": row.get("id") or row.get("batch_index"),
                "deployment_blocking": False,
            }
        )
    return warnings


def _technical_findings_from_checks(
    failed_checks: list[dict[str, Any]],
    warning_checks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if any(check.get("check_id") == "kpi_scope.full_environment_expected_for_limited_simulation" for check in failed_checks):
        first = next(
            check
            for check in failed_checks
            if check.get("check_id") == "kpi_scope.full_environment_expected_for_limited_simulation"
        )
        findings.append(
            {
                "code": "KPI_SCOPE_MISMATCH",
                "severity": "OPERATOR_ACK_REQUIRED",
                "operator_text": (
                    f"The simulation was limited to {first.get('simulated_tooling_count')} tools per line, but some KPI "
                    f"checks still expected the full {first.get('full_environment_tooling_count')}-tool environment."
                ),
            }
        )
    for warning in warning_checks:
        check_id = warning.get("check_id")
        if check_id == "batch_gating.recovered_blocked_next_batch_request":
            findings.append(
                {
                    "code": "RECOVERED_BATCH_GATING_WARNING",
                    "line_id": warning.get("line_id"),
                    "severity": "ATTENTION",
                    "operator_text": str(warning.get("operator_explanation") or ""),
                }
            )
        elif check_id == "placement.incorrect_required_tool":
            findings.append(
                {
                    "code": "PLACEMENT_VALIDATION_WARNING",
                    "line_id": warning.get("line_id"),
                    "severity": "OPERATOR_ACK_REQUIRED",
                    "operator_text": (
                        str(warning.get("operator_explanation") or "")
                        + " This may indicate a placement-validation or sensor issue."
                    ),
                }
            )
    for check in failed_checks:
        if check.get("check_id") == "kpi_scope.full_environment_expected_for_limited_simulation":
            continue
        findings.append(
            {
                "code": str(check.get("technical_check_id") or check.get("check_id") or "UNKNOWN_CHECK"),
                "line_id": check.get("line_id"),
                "severity": "BLOCKING" if check.get("deployment_blocking", True) else str(check.get("severity") or "ATTENTION"),
                "operator_text": str(check.get("operator_explanation") or check.get("operator_label") or "Operational check failed."),
            }
        )
    return findings


def _risk_profile(
    *,
    overall_result: str,
    failure: dict[str, Any] | None,
    failed_checks: list[dict[str, Any]],
    warning_checks: list[dict[str, Any]],
    line_results: list[dict[str, Any]],
) -> dict[str, Any]:
    blocking_checks = [
        check
        for check in failed_checks
        if check.get("deployment_blocking", True)
    ]
    ack_checks = [
        check
        for check in failed_checks
        if not check.get("deployment_blocking", True)
    ]
    ack_warnings = [
        warning
        for warning in warning_checks
        if warning.get("severity") == "WARNING" and warning.get("check_id") == "placement.incorrect_required_tool"
    ]
    attention_warnings = [
        warning
        for warning in warning_checks
        if warning not in ack_warnings
    ]
    if failure and not (failure.get("partial_line_kpis_available") and not blocking_checks):
        risk_tier = "BLOCKING"
    elif blocking_checks:
        risk_tier = "BLOCKING"
    elif ack_checks or ack_warnings:
        risk_tier = "OPERATOR_ACK_REQUIRED"
    elif overall_result == "WARNING" or attention_warnings:
        risk_tier = "ATTENTION"
    else:
        risk_tier = "INFO"

    deployment_allowed = risk_tier in {"INFO", "ATTENTION", "OPERATOR_ACK_REQUIRED"}
    deployment_recommended = risk_tier in {"INFO", "ATTENTION"}
    requires_ack = risk_tier == "OPERATOR_ACK_REQUIRED"
    if risk_tier == "BLOCKING":
        operator_options = ["REQUEST_REVISION", "RERUN_SIMULATION"]
        next_action = "REVISE_OR_RERUN"
    elif requires_ack:
        operator_options = ["DEPLOY_WITH_ACK", "DO_NOT_DEPLOY", "REQUEST_REVISION", "RERUN_SIMULATION"]
        next_action = "ASK_DEPLOY_ACKNOWLEDGEMENT"
    else:
        operator_options = ["DEPLOY", "DO_NOT_DEPLOY", "REQUEST_REVISION"]
        if risk_tier == "ATTENTION":
            operator_options.append("RERUN_SIMULATION")
        next_action = "ASK_DEPLOY_APPROVAL"

    line_findings = []
    for row in line_results:
        line_failed = row.get("failed_checks") or []
        line_warnings = row.get("warnings") or []
        if any(check.get("deployment_blocking", True) for check in line_failed):
            severity = "BLOCKING"
        elif line_failed or any(warning.get("check_id") == "placement.incorrect_required_tool" for warning in line_warnings):
            severity = "OPERATOR_ACK_REQUIRED"
        elif line_warnings:
            severity = "ATTENTION"
        else:
            severity = "INFO"
        line_findings.append(
            {
                "line_id": row.get("line_id"),
                "severity": severity,
                "deployment_blocking": severity == "BLOCKING",
                "operator_text": row.get("operator_reason") or row.get("failure_reason") or "No line-specific finding was recorded.",
            }
        )

    technical_findings = _technical_findings_from_checks(failed_checks, warning_checks)
    acknowledged_risks = [
        str(finding.get("code"))
        for finding in technical_findings
        if finding.get("severity") in {"OPERATOR_ACK_REQUIRED", "ATTENTION"}
    ]
    risk_summary = (
        "The simulation completed the tools shown on each line, but non-blocking risks require operator acknowledgement."
        if requires_ack
        else "Deployment is blocked by one or more operational findings."
        if risk_tier == "BLOCKING"
        else "Evidence supports deployment with minor attention-level observations."
        if risk_tier == "ATTENTION"
        else "Evidence supports deployment."
    )
    return {
        "risk_tier": risk_tier,
        "deployment_allowed": deployment_allowed,
        "deployment_recommended": deployment_recommended,
        "requires_operator_acknowledgement": requires_ack,
        "operator_options": operator_options,
        "next_action": next_action,
        "risk_summary": risk_summary,
        "line_findings": line_findings,
        "technical_findings": technical_findings,
        "acknowledged_risks": list(dict.fromkeys(acknowledged_risks)),
    }


def _line_operator_reason(
    *,
    line_id: str,
    row: dict[str, Any],
    failed_checks: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    priority_required_first: dict[str, Any],
    batch_gating: dict[str, Any],
    placement_status: str,
) -> str:
    completed_count = _int_or_zero(row.get("completed_count"))
    wanted_count = _int_or_zero(row.get("wanted_completed_count"))
    unwanted_count = _int_or_zero(row.get("unwanted_completed_count"))
    scope_check = next(
        (
            check
            for check in failed_checks
            if check.get("check_id") == "kpi_scope.full_environment_expected_for_limited_simulation"
        ),
        None,
    )
    placement_warning = next(
        (warning for warning in warnings if str(warning.get("check_id") or "").startswith("placement.")),
        None,
    )
    gating_warning = next(
        (warning for warning in warnings if warning.get("check_id") == "batch_gating.recovered_blocked_next_batch_request"),
        None,
    )

    parts = [
        f"{line_id} sorted all {completed_count} tools shown in the simulation.",
        f"The completed set contained {wanted_count} required ENT tool{'s' if wanted_count != 1 else ''} and {unwanted_count} unwanted tool{'s' if unwanted_count != 1 else ''}.",
    ]
    if priority_required_first.get("status") == "PASS":
        parts.append("The robot picked the required ENT tools before the unwanted tools available on the table.")
    elif priority_required_first.get("status") == "FAIL":
        parts.append(str(priority_required_first.get("reason") or "The required-first picking rule failed."))
    if batch_gating.get("status") == "PASS":
        parts.append("The table batch completed without a real next-batch gating violation.")
    elif batch_gating.get("status") == "RECOVERED_WARNING" and gating_warning:
        parts.append(str(gating_warning.get("operator_explanation")))
    if placement_status == "WARNING" and placement_warning:
        parts.append(str(placement_warning.get("operator_explanation")))
    elif placement_status == "PASS":
        parts.append("No misplaced tools or entanglements were recorded.")
    if scope_check:
        parts.append(
            "The KPI failure appears to come from the checker expecting the full "
            f"{scope_check.get('full_environment_tooling_count')}-tool environment instead of the "
            f"{scope_check.get('simulated_tooling_count')} tools shown in this run."
        )
    elif failed_checks:
        parts.append("; ".join(str(check.get("operator_explanation")) for check in failed_checks if check.get("operator_explanation")))
    return " ".join(part for part in parts if part)


def _evaluate_line_kpi(
    row: dict[str, Any],
    trt: dict[str, Any],
    scenario_spec: dict[str, Any],
    priority_events: list[dict[str, Any]],
    batch_completion_kpis: list[dict[str, Any]],
    container_completion_events: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
) -> dict[str, Any]:
    line_id = str(row.get("line_id") or "")
    trt_line = (trt.get("lines") or {}).get(line_id) or {}
    policy = _policy_for_line(scenario_spec, line_id)
    priority = policy.get("manipulator_priority") or {}
    target_throughput = _line_target(trt_line, "min_throughput_per_hour")
    max_downtime = _line_target(trt_line, "max_downtime_seconds")
    throughput = _float_or_none(row.get("throughput_per_hour"))
    downtime = _float_or_none(row.get("downtime_seconds"))
    priority_deviation_count = _int_or_zero(row.get("priority_deviation_count"))

    failed_checks: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if target_throughput is None:
        throughput_pass = True
    elif throughput is None:
        throughput_pass = False
        failed_checks.append(
            _failed_check(
                line_id=line_id,
                check_id="throughput.min_per_hour",
                expected=f"at least {_format_number(target_throughput)} tools/hr",
                actual="No throughput_per_hour value was recorded.",
                actual_value=None,
                expected_value=target_throughput,
                evidence_source="line_kpis.throughput_per_hour",
                operator_explanation=f"{line_id} failed because throughput evidence was missing.",
            )
        )
    else:
        throughput_pass = throughput >= target_throughput
        if not throughput_pass:
            failed_checks.append(
                _failed_check(
                    line_id=line_id,
                    check_id="throughput.min_per_hour",
                    expected=f"at least {_format_number(target_throughput)} tools/hr",
                    actual=f"{_format_number(throughput)} tools/hr",
                    actual_value=throughput,
                    expected_value=target_throughput,
                    evidence_source="line_kpis.throughput_per_hour",
                    operator_explanation=(
                        f"{line_id} failed throughput because the simulated rate was "
                        f"{_format_number(throughput)} tools/hr, below the target of {_format_number(target_throughput)} tools/hr."
                    ),
                )
            )

    if max_downtime is None:
        downtime_pass = True
    elif downtime is None:
        downtime_pass = False
        failed_checks.append(
            _failed_check(
                line_id=line_id,
                check_id="downtime.max_seconds",
                expected=f"at most {_format_number(max_downtime)} seconds of downtime",
                actual="No downtime_seconds value was recorded.",
                actual_value=None,
                expected_value=max_downtime,
                evidence_source="line_kpis.downtime_seconds",
                operator_explanation=f"{line_id} failed because downtime evidence was missing.",
            )
        )
    else:
        downtime_pass = downtime <= max_downtime
        if not downtime_pass:
            failed_checks.append(
                _failed_check(
                    line_id=line_id,
                    check_id="downtime.max_seconds",
                    expected=f"at most {_format_number(max_downtime)} seconds of downtime",
                    actual=f"{_format_number(downtime)} seconds",
                    actual_value=downtime,
                    expected_value=max_downtime,
                    evidence_source="line_kpis.downtime_seconds",
                    operator_explanation=(
                        f"{line_id} failed downtime because simulated downtime was "
                        f"{_format_number(downtime)} seconds, above the limit of {_format_number(max_downtime)} seconds."
                    ),
                )
            )

    line_success = _int_or_zero(row.get("success")) == 1
    priority_policy = str(row.get("priority_policy") or priority.get("policy") or "NONE")
    priority_enabled = bool(priority.get("enabled")) or priority_policy not in {"", "NONE", "FCFS"}
    failed_checks.extend(
        _priority_failure_checks(
            line_id=line_id,
            priority_policy=priority_policy,
            priority_enabled=priority_enabled,
            priority_deviation_count=priority_deviation_count,
            priority_events=priority_events,
        )
    )
    failed_checks.extend(_batch_gating_checks(line_id, batch_completion_kpis))
    scope_check = _limited_simulation_scope_check(
        line_id=line_id,
        line_kpi=row,
        scenario_spec=scenario_spec,
        container_completion_events=container_completion_events,
    )
    if scope_check:
        failed_checks.append(scope_check)
    warnings.extend(_placement_warnings(line_id, tool_events))
    warnings.extend(_recovered_batch_gating_warnings(line_id, batch_completion_kpis))
    priority_check_ids = {
        "priority.required_first",
        "priority.policy_missing",
        "result_data.missing_priority_events",
    }
    priority_failed_checks = [check for check in failed_checks if check["check_id"] in priority_check_ids]
    gating_failed_checks = [
        check for check in failed_checks if check["check_id"] == "batch_gating.table_not_empty_before_next_batch"
    ]
    line_priority_events = _priority_events_for_line(priority_events, line_id)
    if priority_policy == "REQUIRED_FIRST" and priority_enabled:
        priority_eval = _evaluate_required_first_priority_events(line_priority_events)
        priority_required_first = {
            "status": "FAIL" if priority_failed_checks else priority_eval["status"],
            "reason": priority_failed_checks[0]["operator_explanation"] if priority_failed_checks else priority_eval["reason"],
        }
    else:
        priority_required_first = {
            "status": "NOT_APPLICABLE",
            "reason": f"{priority_policy} does not require ENT-required tools to be picked first.",
        }
    line_batch_rows = [row for row in batch_completion_kpis if str(row.get("line_id") or "") == line_id]
    if gating_failed_checks:
        batch_gating = {"status": "FAIL", "reason": gating_failed_checks[0]["operator_explanation"]}
    elif any(warning.get("check_id") == "batch_gating.recovered_blocked_next_batch_request" for warning in warnings):
        batch_gating = {
            "status": "RECOVERED_WARNING",
            "reason": next(
                warning["operator_explanation"]
                for warning in warnings
                if warning.get("check_id") == "batch_gating.recovered_blocked_next_batch_request"
            ),
        }
    elif line_batch_rows:
        batch_gating = {
            "status": "PASS",
            "reason": "No next-batch request was recorded while current table tools remained unpicked.",
        }
    else:
        batch_gating = {"status": "NO_DATA", "reason": "No batch completion KPI rows were recorded for this line."}

    placement_status = "WARNING" if any(str(warning.get("check_id") or "").startswith("placement.") for warning in warnings) else "PASS"
    placement_reason = (
        next(
            warning["operator_explanation"]
            for warning in warnings
            if str(warning.get("check_id") or "").startswith("placement.")
        )
        if placement_status == "WARNING"
        else "No misplaced tools or placement validation warnings were recorded."
    )

    if not line_success and not failed_checks:
        failed_checks.append(
            _failed_check(
                line_id=line_id,
                check_id="tooling.wanted_completion",
                expected="The line should complete the required wanted/unwanted tooling classification task.",
                actual="line_kpis.success was 0.",
                actual_value=0,
                expected_value=1,
                evidence_source="line_kpis.success",
                operator_explanation=(
                    f"{line_id} did not meet the line KPI success condition, but no lower-level causal evidence "
                    "was available to explain which operational condition caused it."
                ),
            )
        )
    priority_pass = not any(check["check_id"].startswith("priority.") or check["check_id"].startswith("result_data.missing_priority") for check in failed_checks)
    blocking_line_checks = [check for check in failed_checks if check.get("deployment_blocking", True)]
    status = "PASS" if line_success and throughput_pass and downtime_pass and not failed_checks else "FAIL"
    if failed_checks and not blocking_line_checks:
        status = "OPERATOR_ACK_REQUIRED"
    if not failed_checks and warnings:
        status = "WARNING"

    operator_reason = _line_operator_reason(
        line_id=line_id,
        row=row,
        failed_checks=failed_checks,
        warnings=warnings,
        priority_required_first=priority_required_first,
        batch_gating=batch_gating,
        placement_status=placement_status,
    )

    return {
        "line_id": line_id,
        "status": status,
        "simulated_tools_completed": int(row.get("completed_count") or 0),
        "throughput_per_hour": throughput,
        "target_min_throughput_per_hour": target_throughput,
        "throughput_pass": throughput_pass,
        "downtime_seconds": downtime,
        "max_downtime_seconds": max_downtime,
        "downtime_pass": downtime_pass,
        "wanted_completion_time_seconds": row.get("required_tray_completion_seconds"),
        "unwanted_completion_time_seconds": row.get("unwanted_box_completion_seconds"),
        "wanted_completed_count": int(row.get("wanted_completed_count") or 0),
        "unwanted_completed_count": int(row.get("unwanted_completed_count") or 0),
        "priority_policy": priority_policy,
        "priority_deviation_count": priority_deviation_count,
        "priority_pass": priority_pass,
        "priority_required_first": priority_required_first,
        "batch_gating": batch_gating,
        "placement": {"status": placement_status, "reason": placement_reason},
        "failed_check_ids": [check["check_id"] for check in failed_checks],
        "failed_checks": failed_checks,
        "warnings": warnings,
        "warning_check_ids": [warning["check_id"] for warning in warnings],
        "deployment_blocking": any(check.get("deployment_blocking", True) for check in failed_checks),
        "failure_reason": None if status == "PASS" else "; ".join(check["operator_explanation"] for check in failed_checks),
        "operator_reason": operator_reason,
    }


def _failure_summary(
    run_artifact: dict[str, Any],
    seed_sweep_failure: dict[str, Any] | None,
    host_runner: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if run_artifact.get("status") in {"COMPLETED", "SUCCESS"} and not run_artifact.get("error_code"):
        return None
    run = run_artifact.get("run") or {}
    raw_error = (
        run_artifact.get("root_exception")
        or run_artifact.get("message")
        or run.get("error_message")
        or (seed_sweep_failure or {}).get("failure_reason")
        or (host_runner or {}).get("stderr_tail")
        or (host_runner or {}).get("stdout_tail")
        or "Simulation failed, but no failure_reason was recorded."
    )
    if "Assert validation failed" in str(raw_error) and run_artifact.get("line_kpis"):
        return None
    stage = (
        run_artifact.get("failed_function")
        or run_artifact.get("error_code")
        or (seed_sweep_failure or {}).get("failure_stage")
        or (host_runner or {}).get("status")
        or run.get("status")
        or "unknown"
    )
    return {
        "failure_stage": stage,
        "failure_reason": raw_error,
        "failure_details": run_artifact.get("failure_details") or {},
        "source": "output_db" if run else ("seed_sweep.sqlite3" if seed_sweep_failure else "host_runner"),
        "raw_error_message": raw_error,
        "operator_explanation": f"Simulation failed during {stage}. The recorded reason was: {raw_error}",
        "partial_line_kpis_available": bool(run_artifact.get("line_kpis")),
        "operator_next_action": "REQUEST_REVISION or RERUN_SIMULATION",
    }


def _operator_summary(
    overall_result: str,
    line_results: list[dict[str, Any]],
    failure: dict[str, Any] | None,
    failed_checks: list[dict[str, Any]] | None = None,
) -> str:
    if failure:
        return (
            f"Simulation failed. Failure stage: {failure['failure_stage']}. "
            f"Failure reason: {failure['failure_reason']}. Deployment is not recommended."
        )
    if overall_result == "PASS":
        passed_lines = ", ".join(row["line_id"] for row in line_results if row["status"] == "PASS")
        return (
            f"Simulation completed and KPI evidence supports deployment. "
            f"Passing lines: {passed_lines or 'none'}."
        )
    if failed_checks:
        scope_checks = [
            check
            for check in failed_checks
            if check.get("check_id") == "kpi_scope.full_environment_expected_for_limited_simulation"
        ]
        if scope_checks and len(scope_checks) == len(failed_checks):
            simulated_count = scope_checks[0].get("simulated_tooling_count")
            full_count = scope_checks[0].get("full_environment_tooling_count")
            line_summaries = [
                str(row.get("operator_reason"))
                for row in line_results
                if row.get("operator_reason")
            ]
            return "\n\n".join(
                [
                    (
                        "Simulation completed, and the proposed strategy mostly behaved as expected. Deployment is possible, "
                        "but it requires operator acknowledgement because there are non-blocking risks."
                    ),
                    (
                        "The main issue is not that the robots failed to sort the tools shown in the simulation. "
                        f"The simulation was limited to {simulated_count} tools per line, but the KPI checker still expected "
                        f"the full {full_count}-tool environment to be completed."
                    ),
                    *line_summaries,
                    (
                        "Deployment is allowed with acknowledgement, but not automatically recommended. Recommended next step: "
                        "update the KPI checker so a limited-tool simulation is evaluated against the tools actually shown, "
                        "or clearly mark it as a partial-table run, then rerun the simulation."
                    ),
                ]
            )
        failed_lines = sorted({str(check.get("line_id")) for check in failed_checks if check.get("line_id")})
        labels = list(dict.fromkeys(str(check.get("operator_label")) for check in failed_checks if check.get("operator_label")))
        if len(failed_lines) == 4 and set(failed_lines) == {"line_1", "line_2", "line_3", "line_4"}:
            line_phrase = "All four lines"
        else:
            line_phrase = f"Lines {', '.join(failed_lines)}"
        reason_phrase = "; ".join(labels) if labels else "one or more operational checks failed"
        return (
            f"The simulation completed, but deployment is not recommended. "
            f"{line_phrase} failed KPI validation because: {reason_phrase}."
        )
    failed_lines = ", ".join(row["line_id"] for row in line_results if row["status"] != "PASS")
    return (
        f"Simulation completed, but KPI evidence does not support deployment yet. "
        f"Lines needing attention: {failed_lines or 'unknown'}."
    )


def _expected_simulation_lines(scenario_spec: dict[str, Any], trt: dict[str, Any]) -> list[str]:
    scope_lines = ((scenario_spec.get("simulation_scope") or {}).get("lines") or [])
    if scope_lines:
        return [str(line_id) for line_id in scope_lines]
    bindings = scenario_spec.get("line_bindings") or []
    if bindings:
        return [str(binding.get("line_id")) for binding in bindings if binding.get("line_id")]
    return sorted(str(line_id) for line_id in (trt.get("lines") or {}).keys())


def _missing_line_checks(expected_lines: list[str], actual_lines: set[str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not actual_lines:
        for line_id in expected_lines:
            checks.append(
                _failed_check(
                    line_id=line_id,
                    check_id="result_data.missing_kpi_rows",
                    expected="A line_kpis row for every simulated production line.",
                    actual="No line_kpis row was recorded for this line.",
                    actual_value=0,
                    expected_value=1,
                    evidence_source="output_db.line_kpis",
                    operator_explanation=f"{line_id} failed because no line KPI evidence was recorded.",
                )
            )
        return checks
    for line_id in expected_lines:
        if line_id not in actual_lines:
            checks.append(
                _failed_check(
                    line_id=line_id,
                    check_id="simulation_scope.line_kpis_missing",
                    expected="A line_kpis row for every line in simulation_scope.lines.",
                    actual=f"No line_kpis row was recorded for {line_id}.",
                    actual_value="missing",
                    expected_value="present",
                    evidence_source="output_db.line_kpis",
                    operator_explanation=f"{line_id} failed because simulation scope required KPI evidence for this line, but none was recorded.",
                )
            )
    return checks


def _failure_details_message(failed_checks: list[dict[str, Any]]) -> str:
    if not failed_checks:
        return "KPI validation failed, but no detailed failed checks were recorded."
    scope_checks = [
        check
        for check in failed_checks
        if check.get("check_id") == "kpi_scope.full_environment_expected_for_limited_simulation"
    ]
    if scope_checks and len(scope_checks) == len(failed_checks):
        simulated_count = scope_checks[0].get("simulated_tooling_count")
        full_count = scope_checks[0].get("full_environment_tooling_count")
        return (
            f"KPI validation failed because this run was limited to {simulated_count} tools per line, "
            f"but the completion KPI still expected the full {full_count}-tool environment."
        )
    lines = sorted({str(check.get("line_id")) for check in failed_checks if check.get("line_id")})
    explanations = list(dict.fromkeys(str(check.get("operator_explanation")) for check in failed_checks if check.get("operator_explanation")))
    return (
        f"KPI validation failed for {', '.join(lines)}. "
        f"Operational reasons: {'; '.join(explanations)}."
    )


def build_evidence_summary(
    *,
    repository: TRTRepository,
    run_id: str,
    scenario_spec_id: str | None = None,
    trt_id: str = "trt-demo",
    trt_version: str | None = None,
    scenario_spec_path: str | None = None,
    output_db_path: str | None = None,
    host_runner: dict[str, Any] | None = None,
    source_seed_sweep_db_path: str | None = None,
) -> dict[str, Any]:
    spec_path = _resolve_path(repository, scenario_spec_path) or _find_scenario_spec_path(repository, scenario_spec_id)
    scenario_spec = _load_json(spec_path)
    scenario_spec_id = scenario_spec_id or scenario_spec.get("scenario_spec_id")
    trt_id = trt_id or scenario_spec.get("trt_id") or "trt-demo"
    trt_version = trt_version or scenario_spec.get("trt_version")
    try:
        trt = repository.load_trt(trt_id, trt_version) if trt_version else repository.get_current_trt(trt_id)
    except Exception:
        trt = repository.get_current_trt(trt_id)
        trt_version = trt.get("version")

    db_path = _output_db_path(repository, run_id, output_db_path)
    run_artifact = read_simulation_results(db_path, run_id)
    seed_db_path = source_seed_sweep_db_path or DEFAULT_SEED_SWEEP_DB_PATH
    seed_failure = _latest_seed_sweep_failure(seed_db_path)
    failure = _failure_summary(run_artifact, seed_failure, host_runner)

    line_results = [
        _evaluate_line_kpi(
            row,
            trt,
            scenario_spec,
            run_artifact.get("priority_events") or [],
            run_artifact.get("batch_completion_kpis") or [],
            run_artifact.get("container_completion_events") or [],
            run_artifact.get("tool_events") or [],
        )
        for row in (run_artifact.get("line_kpis") or [])
    ]
    failed_checks: list[dict[str, Any]] = [
        check
        for line_result in line_results
        for check in (line_result.get("failed_checks") or [])
    ]
    expected_lines = _expected_simulation_lines(scenario_spec, trt)
    actual_lines = {str(row.get("line_id")) for row in (run_artifact.get("line_kpis") or []) if row.get("line_id")}
    missing_checks = _missing_line_checks(expected_lines, actual_lines)
    failed_checks.extend(missing_checks)
    if missing_checks:
        line_result_by_id = {row["line_id"]: row for row in line_results}
        for check in missing_checks:
            line_id = check["line_id"]
            line_result_by_id.setdefault(
                line_id,
                {
                    "line_id": line_id,
                    "status": "FAIL",
                    "throughput_per_hour": None,
                    "target_min_throughput_per_hour": _line_target((trt.get("lines") or {}).get(line_id) or {}, "min_throughput_per_hour"),
                    "throughput_pass": None,
                    "downtime_seconds": None,
                    "max_downtime_seconds": _line_target((trt.get("lines") or {}).get(line_id) or {}, "max_downtime_seconds"),
                    "downtime_pass": None,
                    "wanted_completion_time_seconds": None,
                    "unwanted_completion_time_seconds": None,
                    "wanted_completed_count": 0,
                    "unwanted_completed_count": 0,
                    "priority_policy": str((_policy_for_line(scenario_spec, line_id).get("manipulator_priority") or {}).get("policy") or "NONE"),
                    "priority_deviation_count": 0,
                    "priority_pass": None,
                    "failed_check_ids": [],
                    "failed_checks": [],
                    "deployment_blocking": True,
                    "failure_reason": None,
                },
            )
            line_result_by_id[line_id]["failed_checks"].append(check)
            line_result_by_id[line_id]["failed_check_ids"].append(check["check_id"])
            line_result_by_id[line_id]["failure_reason"] = "; ".join(
                item["operator_explanation"] for item in line_result_by_id[line_id]["failed_checks"]
            )
        line_results = [line_result_by_id[line_id] for line_id in sorted(line_result_by_id)]
    critical_failures: list[str] = []
    warning_checks: list[dict[str, Any]] = [
        warning
        for line_result in line_results
        for warning in (line_result.get("warnings") or [])
    ]
    warnings: list[str] = [
        str(warning.get("operator_explanation"))
        for warning in warning_checks
        if warning.get("operator_explanation")
    ]
    if failure:
        critical_failures.append(str(failure["failure_reason"]))
    if not line_results and not failure:
        warnings.append("No line KPI rows were available for evaluation.")
    failed_lines = sorted({str(check.get("line_id")) for check in failed_checks if check.get("line_id")})
    passed_lines = sorted(
        {
            row["line_id"]
            for row in line_results
            if row.get("status") == "PASS" and not any(check.get("line_id") == row.get("line_id") for check in failed_checks)
        }
    )
    inconclusive_lines = sorted(
        {
            row["line_id"]
            for row in line_results
            if row.get("status") != "PASS" and not any(check.get("line_id") == row.get("line_id") for check in failed_checks)
        }
    )
    blocking_failed_checks = [check for check in failed_checks if check.get("deployment_blocking", True)]
    nonblocking_failed_checks = [check for check in failed_checks if not check.get("deployment_blocking", True)]
    if blocking_failed_checks:
        critical_failures.append(_failure_details_message(blocking_failed_checks))
    scope_mismatch_checks = [
        check
        for check in failed_checks
        if check.get("check_id") == "kpi_scope.full_environment_expected_for_limited_simulation"
    ]
    simulated_tooling_count = (
        scope_mismatch_checks[0].get("simulated_tooling_count")
        if scope_mismatch_checks
        else (scenario_spec.get("simulation_config") or {}).get("add_reference_number")
    )
    full_environment_tooling_count = (
        scope_mismatch_checks[0].get("full_environment_tooling_count")
        if scope_mismatch_checks
        else None
    )
    likely_root_cause = (
        {
            "code": "KPI_EXPECTED_FULL_ENVIRONMENT_BUT_SIMULATION_LIMITED",
            "operator_text": (
                f"The simulation was limited to {simulated_tooling_count} tools per line, but the KPI checker still "
                f"expected all {full_environment_tooling_count} tools in the full environment to be sorted."
            ),
        }
        if scope_mismatch_checks
        else None
    )

    if nonblocking_failed_checks:
        warnings.append(_failure_details_message(nonblocking_failed_checks))

    if failure:
        overall_result = "FAIL"
    elif critical_failures:
        overall_result = "FAIL"
    elif failed_checks or warnings:
        overall_result = "WARNING"
    else:
        overall_result = "PASS"
    risk_profile = _risk_profile(
        overall_result=overall_result,
        failure=failure,
        failed_checks=failed_checks,
        warning_checks=warning_checks,
        line_results=line_results,
    )
    deployment_recommended = bool(risk_profile["deployment_recommended"])
    deployment_allowed = bool(risk_profile["deployment_allowed"])
    requires_operator_acknowledgement = bool(risk_profile["requires_operator_acknowledgement"])
    risk_tier = str(risk_profile["risk_tier"])
    next_action = str(risk_profile["next_action"])
    summary_message = (
        "All deterministic KPI checks passed."
        if deployment_recommended
        else _failure_details_message(failed_checks)
    )
    operator_next_action = ", ".join(risk_profile["operator_options"])
    summary = {
        "overall_result": overall_result,
        "deployment_recommended": deployment_recommended,
        "deployment_allowed": deployment_allowed,
        "requires_operator_acknowledgement": requires_operator_acknowledgement,
        "risk_tier": risk_tier,
        "risk_summary": risk_profile["risk_summary"],
        "operator_options": risk_profile["operator_options"],
        "line_findings": risk_profile["line_findings"],
        "technical_findings": risk_profile["technical_findings"],
        "acknowledged_risks": risk_profile["acknowledged_risks"],
        "operator_summary": _operator_summary(overall_result, line_results, failure, failed_checks),
        "kpi_table": line_results,
        "line_results": line_results,
        "failed_lines": failed_lines,
        "passed_lines": passed_lines,
        "inconclusive_lines": inconclusive_lines,
        "failed_checks": failed_checks,
        "warning_checks": warning_checks,
        "simulation_config": {
            "simulated_tooling_count": simulated_tooling_count,
            "full_environment_tooling_count": full_environment_tooling_count,
        },
        "kpi_evaluation_scope": "SIMULATED_TABLE_SCOPE" if scope_mismatch_checks else "FULL_ENVIRONMENT_SCOPE",
        "likely_root_cause": likely_root_cause,
        "summary_message": summary_message,
        "operator_next_action": operator_next_action,
        "failure_summary": failure,
        "warnings": warnings,
        "next_action": next_action,
    }
    canonical_artifact = {
        "run_artifact_id": f"run_artifact_{uuid4()}",
        "run_id": run_id,
        "scenario_spec_id": scenario_spec_id,
        "trt_id": trt_id,
        "trt_version": trt_version or trt.get("version"),
        "status": run_artifact.get("status"),
        "completed_at": (run_artifact.get("run") or {}).get("completed_at"),
        "scenario_spec_path": str(spec_path) if spec_path else None,
        "output_db_path": str(db_path),
        "source_seed_sweep_db_path": seed_db_path,
        "simulation_scope": scenario_spec.get("simulation_scope") or {},
        "line_kpis": run_artifact.get("line_kpis") or [],
        "container_completion_events": run_artifact.get("container_completion_events") or [],
        "line_completion_kpis": run_artifact.get("line_completion_kpis") or [],
        "priority_events": run_artifact.get("priority_events") or [],
        "batch_pick_events": run_artifact.get("batch_pick_events") or [],
        "table_batch_events": run_artifact.get("table_batch_events") or [],
        "batch_completion_kpis": run_artifact.get("batch_completion_kpis") or [],
        "priority_summary": run_artifact.get("priority_summary") or {},
        "failure": failure,
        "kpi_evaluation": {
            "overall_pass": overall_result == "PASS",
            "risk_tier": risk_tier,
            "deployment_allowed": deployment_allowed,
            "requires_operator_acknowledgement": requires_operator_acknowledgement,
            "line_results": line_results,
            "failed_lines": failed_lines,
            "passed_lines": passed_lines,
            "inconclusive_lines": inconclusive_lines,
            "failed_checks": failed_checks,
            "critical_failures": critical_failures,
            "warnings": warnings,
            "warning_checks": warning_checks,
            "kpi_evaluation_scope": "SIMULATED_TABLE_SCOPE" if scope_mismatch_checks else "FULL_ENVIRONMENT_SCOPE",
            "simulation_config": {
                "simulated_tooling_count": simulated_tooling_count,
                "full_environment_tooling_count": full_environment_tooling_count,
            },
            "likely_root_cause": likely_root_cause,
        },
        "deployment_recommendation": {
            "recommended": deployment_recommended,
            "allowed": deployment_allowed,
            "requires_operator_acknowledgement": requires_operator_acknowledgement,
            "risk_tier": risk_tier,
            "operator_options": risk_profile["operator_options"],
            "acknowledged_risks": risk_profile["acknowledged_risks"],
            "reason": risk_profile["risk_summary"],
            "next_action": next_action,
        },
    }
    return {
        "status": "EVIDENCE_READY" if run_artifact.get("status") != "ERROR" or failure else "EVIDENCE_FAILED",
        "run_id": run_id,
        "scenario_spec_id": scenario_spec_id,
        "overall_result": overall_result,
        "deployment_recommended": deployment_recommended,
        "deployment_allowed": deployment_allowed,
        "requires_operator_acknowledgement": requires_operator_acknowledgement,
        "risk_tier": risk_tier,
        "risk_summary": risk_profile["risk_summary"],
        "operator_options": risk_profile["operator_options"],
        "line_findings": risk_profile["line_findings"],
        "technical_findings": risk_profile["technical_findings"],
        "acknowledged_risks": risk_profile["acknowledged_risks"],
        "failed_lines": failed_lines,
        "passed_lines": passed_lines,
        "inconclusive_lines": inconclusive_lines,
        "failed_checks": failed_checks,
        "line_results": line_results,
        "summary_message": summary_message,
        "operator_next_action": operator_next_action,
        "evidence_summary": summary,
        "raw_evidence": {
            "run_artifact": canonical_artifact,
            "source_run_artifact": run_artifact,
            "seed_sweep_latest_failure": seed_failure,
            "generated_at": _now_utc(),
        },
        "errors": critical_failures,
    }
