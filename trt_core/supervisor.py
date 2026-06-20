"""Supervisor state reconciliation for released TRT versions."""

from __future__ import annotations

import logging
from typing import Any

from trt_core.reconciliation import build_reconciliation_plan, save_plan
from trt_core.line_registry import load_line_registry
from trt_core.repository import TRTRepository
from trt_core.state_records import validate_state_records

logger = logging.getLogger(__name__)

LINE_COMPARISON_FIELDS = {
    "goal",
    "allowed_instruments",
    "excluded_instruments",
    "selected_tool_ids",
    "excluded_tool_ids",
    "required_tool_ids",
    "target_set_id",
    "tooling_policy",
    "priority",
    "kpi",
    "abnormal_strategy",
    "digital_twin",
}


def reconcile_current_trt(
    state_records: list[dict[str, Any]],
    repository: TRTRepository | None = None,
    trt_id: str | None = None,
    trt_version: str | None = None,
    release_id: str | None = None,
    affected_lines: list[str] | None = None,
) -> dict[str, Any]:
    reasons = validate_state_records(state_records)
    if reasons:
        raise ValueError("; ".join(reasons))
    repo = repository or TRTRepository()
    current_trt = _load_reconciliation_trt(repo, trt_id, trt_version)
    previous_trt = _load_previous_trt(repo, current_trt)
    state_by_line = {record["line_id"]: record for record in state_records}
    registry = load_line_registry(repo)
    registry_line_ids = set(registry["lines"])
    enabled_registry_line_ids = {
        line_id for line_id, line in registry["lines"].items() if line.get("enabled") is True
    }
    trt_line_ids = set((current_trt.get("lines") or {}).keys())
    state_line_ids = set(state_by_line)
    missing_registry_for_trt = sorted(trt_line_ids - registry_line_ids)
    unknown_state_lines = sorted(state_line_ids - registry_line_ids)
    if missing_registry_for_trt or unknown_state_lines:
        raise ValueError(
            "Line registry mismatch: "
            f"missing_registry_lines={missing_registry_for_trt}; "
            f"unknown_state_lines={unknown_state_lines}"
        )
    reconciliation_line_ids = sorted(trt_line_ids & enabled_registry_line_ids)
    logger.info("supervisor.loaded_trt.version=%r", current_trt.get("version"))
    logger.info("supervisor.loaded_trt.lines=%r", sorted((current_trt.get("lines") or {}).keys()))
    logger.info("supervisor.current_state.lines=%r", sorted(state_by_line.keys()))
    missing_state = _missing_required_state_lines(current_trt, previous_trt, state_by_line, reconciliation_line_ids)
    if missing_state:
        raise ValueError(f"Missing runtime state records for changed TRT lines: {', '.join(missing_state)}")

    previous_lines = previous_trt.get("lines", {}) if previous_trt else {}
    decisions = [
        decide_line(line_id, target_line, previous_lines.get(line_id), state_by_line.get(line_id))
        for line_id, target_line in sorted(current_trt.get("lines", {}).items())
        if line_id in reconciliation_line_ids
    ]
    plan = build_reconciliation_plan(
        trt=current_trt,
        state_records=state_records,
        line_decisions=decisions,
        release_id=release_id,
        affected_lines=affected_lines,
    )
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
    logger.info(
        "supervisor.line.trt_fields line_id=%s fields=%r",
        line_id,
        {
            "goal": target_line.get("goal"),
            "priority": target_line.get("priority"),
            "selected_tool_ids": target_line.get("selected_tool_ids"),
            "excluded_tool_ids": target_line.get("excluded_tool_ids"),
            "allowed_instruments": target_line.get("allowed_instruments"),
            "excluded_instruments": target_line.get("excluded_instruments"),
            "tooling_policy": target_line.get("tooling_policy"),
        },
    )
    logger.info(
        "supervisor.line.runtime_fields line_id=%s fields=%r",
        line_id,
        {
            "mode": state.get("mode"),
            "current_task": state.get("current_task"),
            "wip_count": state.get("wip_count"),
            "current_instruments": state.get("current_instruments"),
            "selected_tool_ids": state.get("selected_tool_ids"),
            "pending_tool_ids": state.get("pending_tool_ids"),
            "completed_tool_ids": state.get("completed_tool_ids"),
            "entanglement": state.get("entanglement"),
        },
    )
    if not changed_fields:
        return line_decision(line_id, "NO_CHANGE", "Target TRT does not change this line.", None, None, [], "No supervisor action required.")

    mode = state["mode"]
    wip_count = state["wip_count"]
    excluded_in_wip = sorted(_excluded_items_in_runtime(target_line, state))
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
    runtime_tool_ids = _runtime_tool_ids(state_record)
    if set(target_line.get("excluded_tool_ids", [])) & runtime_tool_ids:
        changed.add("excluded_tool_ids")
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


def _load_reconciliation_trt(repo: TRTRepository, trt_id: str | None, trt_version: str | None) -> dict[str, Any]:
    if trt_id and trt_version:
        return repo.load_trt(trt_id, trt_version)
    if trt_id:
        records = repo.list_trt_version_records(trt_id)
        if records:
            latest = max(records, key=lambda record: repo._version_number(record["version"]))
            return repo.load_trt(latest["trt_id"], latest["version"])
    return repo.get_current_trt(trt_id)


def _missing_required_state_lines(
    current_trt: dict[str, Any],
    previous_trt: dict[str, Any] | None,
    state_by_line: dict[str, dict[str, Any]],
    reconciliation_line_ids: list[str] | None = None,
) -> list[str]:
    missing: list[str] = []
    previous_lines = previous_trt.get("lines", {}) if previous_trt else {}
    required_line_ids = set(reconciliation_line_ids or (current_trt.get("lines") or {}).keys())
    for line_id, target_line in sorted((current_trt.get("lines") or {}).items()):
        if line_id not in required_line_ids:
            continue
        previous_line = previous_lines.get(line_id)
        if previous_trt is None:
            changed_fields = set(LINE_COMPARISON_FIELDS)
        elif previous_line is None:
            changed_fields = set(LINE_COMPARISON_FIELDS)
        else:
            changed_fields = changed_line_fields(previous_line, target_line)
        if changed_fields and line_id not in state_by_line:
            missing.append(line_id)
    return missing


def _runtime_tool_ids(state_record: dict[str, Any]) -> set[str]:
    tool_ids: set[str] = set()
    for field in ("selected_tool_ids", "pending_tool_ids", "completed_tool_ids", "current_tool_ids"):
        values = state_record.get(field, [])
        if isinstance(values, list):
            tool_ids.update(value for value in values if isinstance(value, str))
    entanglement = state_record.get("entanglement")
    if isinstance(entanglement, dict) and isinstance(entanglement.get("tool_ids"), list):
        tool_ids.update(value for value in entanglement["tool_ids"] if isinstance(value, str))
    return tool_ids


def _excluded_items_in_runtime(target_line: dict[str, Any], state_record: dict[str, Any]) -> set[str]:
    excluded_instruments = set(target_line.get("excluded_instruments", [])) & set(state_record.get("current_instruments", []))
    excluded_tool_ids = set(target_line.get("excluded_tool_ids", [])) & _runtime_tool_ids(state_record)
    return excluded_instruments | excluded_tool_ids
