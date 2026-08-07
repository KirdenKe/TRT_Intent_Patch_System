"""Canonical checkpoint, outcome, timing, and failure definitions for experiments."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable


CHECKPOINTS = {
    "CP0": {
        "test_object": "Operator input validity",
        "pass_criteria": "Input is interpretable and belongs to a supported query or change-request route.",
        "mode": "AUTOMATED_WITH_MANUAL_AUDIT",
    },
    "CP1": {
        "test_object": "Intent recognition and required fields",
        "pass_criteria": "Intent class is correct and all route-specific required fields are complete.",
        "mode": "AUTOMATED_WITH_MANUAL_AUDIT",
    },
    "CP2": {
        "test_object": "Structured output and schema",
        "pass_criteria": "JSON parses and validates against the applicable schema.",
        "mode": "AUTOMATED",
    },
    "CP3": {
        "test_object": "Task and scenario semantics",
        "pass_criteria": "Lines, tasks, equipment, values, and ordering preserve operator meaning.",
        "mode": "AUTOMATED_WITH_MANUAL_AUDIT",
    },
    "CP4": {
        "test_object": "Digital twin executability",
        "pass_criteria": "ScenarioSpec compiles and Isaac returns a finalized RunArtifact.",
        "mode": "AUTOMATED",
    },
    "CP5": {
        "test_object": "KPI, placement, reset, and safety constraints",
        "pass_criteria": "All mandatory evidence checks pass and no deployment-blocking evidence is missing.",
        "mode": "AUTOMATED",
    },
    "CP6": {
        "test_object": "Human review",
        "pass_criteria": "Operator accepts the evidence-backed result without correcting the generated strategy.",
        "mode": "MANUAL",
    },
}

OUTCOME_CLASSES = {
    "AUTONOMOUS_SUCCESS",
    "MANUALLY_ASSISTED_SUCCESS",
    "VALIDATION_FAILURE",
    "INPUT_FAILURE",
    "SIMULATION_FAILURE",
    "SYSTEM_ERROR",
    "MANUAL_REJECTION",
    "EVALUATION_INCOMPLETE",
}

FAILURE_CAUSES = {
    "UNCLEAR_OPERATOR_INPUT",
    "INCORRECT_INTENT_CLASSIFICATION",
    "REQUIRED_FIELD_OMISSION",
    "JSON_SCHEMA_ERROR",
    "TARGET_DEVICE_CONVERSION_OMISSION",
    "TIME_FIELD_SEMANTIC_MISMATCH",
    "PRIORITY_RULE_NOT_ENFORCED",
    "INVALID_PRODUCTION_LINE_NOT_INTERCEPTED",
    "QUERY_CHANGE_ROUTE_CONFUSION",
    "NUMERIC_RANGE_INSUFFICIENT",
    "POLICY_INFEASIBLE",
    "DIGITAL_TWIN_SCENARIO_ISSUE",
    "SIMULATOR_OR_API_ERROR",
    "AUTOMATED_FALSE_POSITIVE",
    "MANUAL_REJECTION",
    "EVALUATION_EVIDENCE_MISSING",
}

TIMING_FIELDS = (
    "llm_generation_seconds",
    "specification_parsing_seconds",
    "environment_wait_seconds",
    "simulation_startup_seconds",
    "reset_seconds",
    "strategy_simulation_seconds",
    "kpi_calculation_seconds",
    "automated_verification_seconds",
    "manual_review_seconds",
    "end_to_end_seconds",
)


def rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def classify_outcome(
    checkpoints: dict[str, bool | None],
    *,
    manual_correction_used: bool = False,
    operator_accepted: bool | None = None,
    system_error: bool = False,
) -> str:
    if system_error:
        return "SYSTEM_ERROR"
    if checkpoints.get("CP0") is False or checkpoints.get("CP1") is False or checkpoints.get("CP2") is False:
        return "INPUT_FAILURE"
    if checkpoints.get("CP4") is False:
        return "SIMULATION_FAILURE"
    if checkpoints.get("CP3") is False or checkpoints.get("CP5") is False:
        return "VALIDATION_FAILURE"
    if operator_accepted is False:
        return "MANUAL_REJECTION"
    if all(checkpoints.get(cp) is True for cp in ("CP0", "CP1", "CP2", "CP3", "CP4", "CP5")):
        return "MANUALLY_ASSISTED_SUCCESS" if manual_correction_used else "AUTONOMOUS_SUCCESS"
    return "EVALUATION_INCOMPLETE"


def automated_result_from_status(status: Any) -> str | None:
    normalized = str(status or "").strip().upper()
    if normalized in {"PASS", "REJECTED"}:
        return "PASS"
    if normalized.startswith("FAIL"):
        return "FAIL"
    return None


def failure_cause_code(stage: Any, detail: Any = "") -> str | None:
    normalized_stage = str(stage or "").strip().lower()
    normalized_detail = str(detail or "").strip().lower()
    if not normalized_stage or normalized_stage == "completed":
        return None
    if normalized_stage in {"runner_exception", "backend_injection"}:
        return "SIMULATOR_OR_API_ERROR"
    if normalized_stage in {"scenario_spec", "scenario_generation"}:
        if "schema" in normalized_detail:
            return "JSON_SCHEMA_ERROR"
        return "DIGITAL_TWIN_SCENARIO_ISSUE"
    if normalized_stage in {"simulation", "isaac_runtime"}:
        return "SIMULATOR_OR_API_ERROR"
    if normalized_stage in {"expected_field_validation", "semantic_validation"}:
        if "priority" in normalized_detail:
            return "PRIORITY_RULE_NOT_ENFORCED"
        if "time" in normalized_detail or "travel" in normalized_detail or "resume" in normalized_detail:
            return "TIME_FIELD_SEMANTIC_MISMATCH"
        if "line" in normalized_detail:
            return "TARGET_DEVICE_CONVERSION_OMISSION"
        return "POLICY_INFEASIBLE"
    if normalized_stage in {"intent_validation", "dialogue"}:
        if "required" in normalized_detail or "missing" in normalized_detail:
            return "REQUIRED_FIELD_OMISSION"
        return "UNCLEAR_OPERATOR_INPUT"
    if normalized_stage in {"tool_orchestration", "config_query", "query_response"}:
        return "QUERY_CHANGE_ROUTE_CONFUSION"
    if normalized_stage in {"data_quality", "unknown"} or "did not expose" in normalized_detail:
        return "EVALUATION_EVIDENCE_MISSING"
    if normalized_stage == "deployment":
        return "MANUAL_REJECTION"
    return "POLICY_INFEASIBLE"


def derive_checkpoint_record(
    *,
    suite: str,
    prompt: str,
    expected_status: str,
    should_launch_isaac: bool,
    scenario_spec_id: str | None,
    run_artifact_exists: bool,
    packet_score: dict[str, Any],
    turn_labels: Iterable[str] = (),
    system_error: bool = False,
) -> dict[str, Any]:
    """Project only defensible checkpoint facts from one automated execution.

    CP6 is intentionally never inferred here. A human-review result must be
    supplied by an operator or reviewer in a separate field.
    """

    status = str(packet_score.get("status") or "")
    checks = packet_score.get("checks") or {}
    expected = str(expected_status or "").upper()
    automated_result = automated_result_from_status(status)
    valid_routes = {"REVIEWED", "ANSWER_READY", "HELP", "CANCELLED"}

    if not str(prompt or "").strip():
        cp0: bool | None = False
    elif suite == "TC4" or expected not in valid_routes:
        cp0 = False if automated_result == "PASS" else None
    else:
        cp0 = True

    if suite in {"TC1", "TC3"}:
        if expected == "REVIEWED":
            cp1 = True if scenario_spec_id else (False if automated_result == "FAIL" else None)
        elif expected in {"ANSWER_READY", "HELP", "CANCELLED"}:
            cp1 = automated_result == "PASS" if automated_result is not None else None
        else:
            cp1 = False if automated_result == "PASS" else None
    elif suite == "TC2":
        cp1 = automated_result == "PASS" if automated_result is not None else None
    else:
        cp1 = None

    cp2 = checks.get("scenario_spec_schema_pass")
    if cp2 is None and scenario_spec_id:
        cp2 = True

    semantic_keys = (
        "target_line_match",
        "kpi_update_match",
        "simulation_config_match",
        "launch_parameter_match",
        "tooling_policy_match",
        "manipulator_priority_match",
    )
    semantic_values = [checks[key] for key in semantic_keys if isinstance(checks.get(key), bool)]
    cp3 = all(semantic_values) if semantic_values else None

    cp4 = None
    if should_launch_isaac:
        cp4 = run_artifact_exists

    cp5_values = [
        checks[key]
        for key in (
            "placement_verification_pass",
            "reset_completion_pass",
            "kpi_compliance_pass",
            "deployment_guardrail_pass",
        )
        if isinstance(checks.get(key), bool)
    ]
    cp5 = all(cp5_values) if cp5_values else None
    checkpoints = {
        "CP0": cp0,
        "CP1": cp1,
        "CP2": cp2 if isinstance(cp2, bool) else None,
        "CP3": cp3,
        "CP4": cp4,
        "CP5": cp5,
        "CP6": None,
    }
    labels = {str(label).lower() for label in turn_labels}
    manual_correction_used = bool(labels & {"correction", "revision", "edit"})
    manual_intervention_required = manual_correction_used
    outcome_class = classify_outcome(
        checkpoints,
        manual_correction_used=manual_correction_used,
        operator_accepted=None,
        system_error=system_error,
    )
    return {
        **checkpoints,
        "automated_result": automated_result,
        "manual_result": None,
        "human_reviewed": False,
        "manual_correction_used": manual_correction_used,
        "manual_intervention_required": manual_intervention_required,
        "outcome_class": outcome_class,
        "failure_cause_code": failure_cause_code(
            packet_score.get("failure_stage"),
            packet_score.get("failure_cause"),
        ),
        "rejection_reason": None,
        "correction_method": None,
    }


def completion_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, float | int | None]:
    records = list(rows)
    total = len(records)
    autonomous = sum(row.get("outcome_class") == "AUTONOMOUS_SUCCESS" for row in records)
    assisted = sum(row.get("outcome_class") == "MANUALLY_ASSISTED_SUCCESS" for row in records)
    requiring_help = sum(bool(row.get("manual_intervention_required")) for row in records)
    ultimately_completed = autonomous + assisted
    return {
        "total_cases": total,
        "autonomous_success_count": autonomous,
        "assisted_success_count": assisted,
        "autonomous_success_rate": rate(autonomous, total),
        "assisted_completion_rate": rate(assisted, requiring_help),
        "overall_completion_rate": rate(ultimately_completed, total),
    }


def auto_human_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = list(rows)
    automated = [row for row in records if row.get("automated_result") in {"PASS", "FAIL"}]
    reviewed = [row for row in records if row.get("manual_result") in {"PASS", "FAIL"}]
    agreement = [row for row in reviewed if row.get("automated_result") == row.get("manual_result")]
    return {
        "automated_pass_rate": rate(sum(row["automated_result"] == "PASS" for row in automated), len(automated)),
        "manual_verification_pass_rate": rate(sum(row["manual_result"] == "PASS" for row in reviewed), len(reviewed)),
        "auto_human_agreement_rate": rate(len(agreement), len(reviewed)),
        "automated_pass_manual_fail": [
            row.get("test_id") for row in reviewed
            if row.get("automated_result") == "PASS" and row.get("manual_result") == "FAIL"
        ],
        "automated_fail_manual_pass": [
            row.get("test_id") for row in reviewed
            if row.get("automated_result") == "FAIL" and row.get("manual_result") == "PASS"
        ],
    }


def elapsed_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return max(0.0, (end_dt - start_dt).total_seconds())
