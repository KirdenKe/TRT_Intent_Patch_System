"""Supervisor state reconciliation for released TRT versions."""

from __future__ import annotations

from typing import Any

from trt_core.reconciliation import build_reconciliation_plan, save_plan
from trt_core.repository import TRTRepository
from trt_core.state_records import validate_state_records


LINE_COMPARISON_FIELDS = {
    "goal",
    "allowed_instruments",
    "excluded_instruments",
    "priority",
    "kpi",
    "abnormal_strategy",
}


def reconcile_current_trt(
    state_records: list[dict[str, Any]],
    repository: TRTRepository | None = None,
    trt_id: str | None = None,
) -> dict[str, Any]:
    reasons = validate_state_records(state_records)
    if reasons:
        raise ValueError("; ".join(reasons))
    repo = repository or TRTRepository()
    current_trt = repo.get_current_trt(trt_id)
    previous_trt = _load_previous_trt(repo, current_trt)
    state_by_line = {record["line_id"]: record for record in state_records}
    decisions = [
        decide_line(line_id, target_line, previous_trt.get("lines", {}).get(line_id), state_by_line.get(line_id))
        for line_id, target_line in sorted(current_trt.get("lines", {}).items())
    ]
    plan = build_reconciliation_plan(trt=current_trt, state_records=state_records, line_decisions=decisions)
    return save_plan(plan, repo)


def decide_line(
    line_id: str,
    target_line: dict[str, Any],
    previous_line: dict[str, Any] | None,
    state_record: dict[str, Any] | None,
) -> dict[str, Any]:
    state = state_record or {
        "line_id": line_id,
        "mode": "ERROR",
        "current_task": None,
        "wip_count": 0,
        "current_instruments": [],
        "checkpoint": "NONE",
        "last_exception": "missing_state_record",
    }
    changed_fields = changed_line_fields(previous_line, target_line) if previous_line is not None else inferred_changed_fields(state, target_line)
    if not changed_fields:
        return line_decision(line_id, "NO_CHANGE", "Target TRT does not change this line.", None, None, [], "No supervisor action required.")

    mode = state["mode"]
    wip_count = state["wip_count"]
    excluded_in_wip = sorted(set(target_line.get("excluded_instruments", [])) & set(state.get("current_instruments", [])))
    only_priority = changed_fields == {"priority"}
    priority_plus_instrument_delay = "priority" in changed_fields and bool(excluded_in_wip)

    if mode == "ERROR":
        return line_decision(
            line_id,
            "REJECT_INCOMPATIBLE",
            "Line is in ERROR mode and cannot accept a target switch.",
            None,
            None,
            ["line_error"],
            "Resolve the line error before applying this TRT change.",
        )

    if mode == "INTERVENTION":
        return line_decision(
            line_id,
            "WAIT_FOR_CHECKPOINT",
            "Line is under intervention and requires manual clearance.",
            "MANUAL_CLEARANCE_REQUIRED",
            None,
            ["manual_intervention"],
            "Wait for manual clearance before switching.",
        )

    if only_priority:
        return line_decision(
            line_id,
            "IMMEDIATE_SWITCH",
            "Only priority changes; no instrument or goal transition is required.",
            None,
            None,
            [],
            "Apply priority update immediately.",
        )

    if priority_plus_instrument_delay and wip_count > 0:
        return line_decision(
            line_id,
            "DEGRADED_SWITCH",
            "Priority can be applied now, but instrument restrictions must wait for WIP checkpoint.",
            "TRAY_COMPLETE",
            "APPLY_PRIORITY_ONLY_DELAY_INSTRUMENT_RESTRICTIONS",
            ["excluded_instrument_in_wip"],
            "Apply priority now and defer instrument restrictions until current WIP clears.",
        )

    if excluded_in_wip and wip_count > 0:
        return line_decision(
            line_id,
            "WAIT_FOR_CHECKPOINT",
            "Target excludes instruments currently present in WIP.",
            "TRAY_COMPLETE",
            None,
            ["excluded_instrument_in_wip"],
            "Wait until current tray or batch reaches a safe checkpoint.",
        )

    if mode == "IDLE":
        return line_decision(line_id, "IMMEDIATE_SWITCH", "Line is idle.", None, None, [], "Switch line to target TRT immediately.")

    if mode == "RUNNING" and wip_count == 0:
        return line_decision(
            line_id,
            "IMMEDIATE_SWITCH",
            "Line is running with no WIP.",
            None,
            None,
            [],
            "Switch line to target TRT immediately.",
        )

    if mode == "RUNNING" and wip_count > 0:
        return line_decision(
            line_id,
            "WAIT_FOR_CHECKPOINT",
            "Line is running with active WIP.",
            "TRAY_COMPLETE",
            None,
            ["active_wip"],
            "Wait for a checkpoint before switching.",
        )

    return line_decision(
        line_id,
        "WAIT_FOR_CHECKPOINT",
        f"Line mode {mode} requires a checkpoint before switching.",
        "TRAY_COMPLETE",
        None,
        ["paused_or_unknown_transition"],
        "Wait for a safe checkpoint before switching.",
    )


def changed_line_fields(previous_line: dict[str, Any] | None, target_line: dict[str, Any]) -> set[str]:
    if previous_line is None:
        return set(LINE_COMPARISON_FIELDS)
    return {field for field in LINE_COMPARISON_FIELDS if previous_line.get(field) != target_line.get(field)}


def inferred_changed_fields(state_record: dict[str, Any], target_line: dict[str, Any]) -> set[str]:
    changed: set[str] = set()
    if state_record.get("current_task") != target_line.get("goal"):
        changed.add("goal")
    if set(target_line.get("excluded_instruments", [])) & set(state_record.get("current_instruments", [])):
        changed.add("excluded_instruments")
    return changed


def line_decision(
    line_id: str,
    decision: str,
    reason: str,
    required_checkpoint: str | None,
    degraded_strategy: str | None,
    risk_flags: list[str],
    next_action: str,
) -> dict[str, Any]:
    return {
        "line_id": line_id,
        "decision": decision,
        "reason": reason,
        "required_checkpoint": required_checkpoint,
        "degraded_strategy": degraded_strategy,
        "risk_flags": risk_flags,
        "next_action": next_action,
    }


def _load_previous_trt(repo: TRTRepository, current_trt: dict[str, Any]) -> dict[str, Any] | None:
    version = current_trt.get("version", "")
    if not version.startswith("v"):
        return None
    try:
        version_number = int(version[1:])
    except ValueError:
        return None
    if version_number <= 1:
        return None
    try:
        return repo.load_trt(current_trt["trt_id"], f"v{version_number - 1}")
    except Exception:
        return None

