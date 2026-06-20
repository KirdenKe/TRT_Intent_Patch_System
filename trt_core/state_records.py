"""State record persistence and validation for production lines."""

from __future__ import annotations

from typing import Any

from trt_core.repository import TRTRepository


VALID_MODES = {"IDLE", "RUNNING", "INTERVENTION", "PAUSED", "ERROR"}
VALID_TASKS = {"ROUTINE_CLASSIFICATION", "TRAUMA_SET_PRIORITY", "BACKLOG_CLEARING", None}
VALID_INSTRUMENTS = {"SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"}
VALID_CHECKPOINTS = {"NONE", "TRAY_COMPLETE", "BATCH_COMPLETE", "MANUAL_CLEARANCE_REQUIRED"}
VALID_TOOL_IDS = {f"tool_{index:02d}" for index in range(1, 28)}


def validate_state_records(state_records: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    seen: set[str] = set()
    for index, record in enumerate(state_records):
        line_id = record.get("line_id")
        if not isinstance(line_id, str) or not line_id:
            reasons.append(f"state record {index}: line_id is required")
        elif line_id in seen:
            reasons.append(f"state record {index}: duplicate line_id {line_id}")
        seen.add(line_id)

        if record.get("mode") not in VALID_MODES:
            reasons.append(f"state record {index}: invalid mode {record.get('mode')!r}")
        if record.get("current_task") not in VALID_TASKS:
            reasons.append(f"state record {index}: invalid current_task {record.get('current_task')!r}")
        if not isinstance(record.get("wip_count"), int) or record.get("wip_count") < 0:
            reasons.append(f"state record {index}: wip_count must be non-negative integer")
        if record.get("checkpoint") not in VALID_CHECKPOINTS:
            reasons.append(f"state record {index}: invalid checkpoint {record.get('checkpoint')!r}")
        for instrument in record.get("current_instruments", []):
            if instrument not in VALID_INSTRUMENTS:
                reasons.append(f"state record {index}: invalid current instrument {instrument!r}")
        if not isinstance(record.get("locked_resources", []), list):
            reasons.append(f"state record {index}: locked_resources must be a list")
        for field in ("selected_tool_ids", "completed_tool_ids", "pending_tool_ids"):
            values = record.get(field, [])
            if not isinstance(values, list):
                reasons.append(f"state record {index}: {field} must be a list")
                continue
            for tool_id in values:
                if tool_id not in VALID_TOOL_IDS:
                    reasons.append(f"state record {index}: invalid {field} tool_id {tool_id!r}")
        entanglement = record.get("entanglement")
        if entanglement is not None:
            if not isinstance(entanglement, dict):
                reasons.append(f"state record {index}: entanglement must be an object")
            else:
                if not isinstance(entanglement.get("detected"), bool):
                    reasons.append(f"state record {index}: entanglement.detected must be a boolean")
                if not isinstance(entanglement.get("requires_operator"), bool):
                    reasons.append(f"state record {index}: entanglement.requires_operator must be a boolean")
                tool_ids = entanglement.get("tool_ids", [])
                if not isinstance(tool_ids, list):
                    reasons.append(f"state record {index}: entanglement.tool_ids must be a list")
                else:
                    for tool_id in tool_ids:
                        if tool_id not in VALID_TOOL_IDS:
                            reasons.append(f"state record {index}: invalid entanglement tool_id {tool_id!r}")
    return reasons


def save_current_state(state_records: list[dict[str, Any]], repository: TRTRepository | None = None) -> list[dict[str, Any]]:
    reasons = validate_state_records(state_records)
    if reasons:
        raise ValueError("; ".join(reasons))
    repo = repository or TRTRepository()
    repo.save_state_records(state_records)
    return state_records


def load_current_state(repository: TRTRepository | None = None) -> list[dict[str, Any]]:
    repo = repository or TRTRepository()
    return repo.load_state_records()
