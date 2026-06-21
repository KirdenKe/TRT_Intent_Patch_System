"""Deterministic intent-text pre-checks for LLM candidate review."""

from __future__ import annotations

import re
from typing import Any


GOAL_PATTERNS = {
    "TRAUMA_SET_PRIORITY": [r"\btrauma\b", r"\burgent\b"],
    "BACKLOG_CLEARING": [r"\bbacklog\b", r"\bclear backlog\b"],
    "ROUTINE_CLASSIFICATION": [r"\broutine\b", r"\bnormal\b"],
}
SUPPORTED_INSTRUMENT_TERMS = {
    "SCISSORS": ["scissor", "scissors"],
    "FORCEPS": ["forceps"],
    "CLAMPS": ["clamp", "clamps"],
    "RETRACTOR": ["retractor", "retractors"],
}
KPI_TERMS = [
    "deadline",
    "downtime",
    "kpi",
    "throughput",
    "throughput/hr",
    "throughput per hour",
    "min throughput",
    "minimum throughput",
]
PRIORITY_TERMS = ["highest priority", "lowest priority", "priority", "highest level"]
TOOLING_POLICY_TERMS = ["tooling", "tool", "tools", "instrument", "instruments"]
INSTRUMENT_SCOPE_TERMS = ["allow", "allowed", "select", "selected", "exclude", "excluded", "required", "mandatory"]
MANIPULATOR_PRIORITY_TERMS = [
    "pick",
    "pick up",
    "pickup",
    "grasp",
    "grasp order",
    "picking order",
    "required tools first",
    "unwanted tools first",
    "ent required tools first",
    "ent-required tools first",
    "focus on the ent surgical tooling set",
    "focus on ent surgical tooling set",
]
SIMULATION_CONFIG_TERMS = [
    "add_reference_number",
    "add reference number",
    "allowed_overlap_ratio",
    "allowed overlap ratio",
    "chosen_intervention_mode",
    "chosen intervention mode",
    "episode_success_requires_reset_cycles",
    "successful reset cycles",
    "global_seed",
    "global seed",
    "travel_time",
    "travel time",
    "operator travel time",
    "fix_duration",
    "fix duration",
    "resume_delay",
    "resume delay",
    "headless",
    "rendering enabled",
    "immediate stop",
    "continue until operator arrival",
]
UNSUPPORTED_INSTRUMENT_TERMS = ["laser scalpel", "scalpel", "stapler", "staplers"]
READONLY_STATE_TERMS = ["pause", "paused", "running", "error mode", "last exception", "wip", "state", "mode"]
RESTRICTED_SIMULATION_SETTINGS = {
    "layout_source": "layout_source is an infrastructure simulation setting and cannot be changed through normal operator requests.",
    "layout source": "layout_source is an infrastructure simulation setting and cannot be changed through normal operator requests.",
    "max_seed_trials": "max_seed_trials is an internal developer sweep parameter and cannot be changed through normal operator requests.",
    "max seed trials": "max_seed_trials is an internal developer sweep parameter and cannot be changed through normal operator requests.",
    "seed_db_path": "seed_db_path is infrastructure configuration and cannot be changed through normal operator requests.",
    "seed db path": "seed_db_path is infrastructure configuration and cannot be changed through normal operator requests.",
    "reuse_precomputed_layouts": "reuse_precomputed_layouts is an internal layout-cache setting and cannot be changed through normal operator requests.",
    "reuse precomputed layouts": "reuse_precomputed_layouts is an internal layout-cache setting and cannot be changed through normal operator requests.",
}
ALL_LINE_TERMS = ["every line", "all lines", "each line", "line 1 and line 2", "lines 1 and 2", "line one and line two"]
SPELLED_LINES = {"one": 1, "two": 2, "three": 3, "four": 4, "nine": 9}


def _normalize_text(text: str) -> str:
    return text.lower().replace("-", " ")


def _line_mentions(text: str) -> list[int]:
    normalized = _normalize_text(text)
    mentions = []
    for match in re.finditer(r"\b(?:line|lines|production line|production lines)\s+((?:\d+\s*(?:,|and)?\s*)+)", normalized):
        mentions.extend(int(number) for number in re.findall(r"\d+", match.group(1)))
    for word, number in SPELLED_LINES.items():
        if re.search(rf"\b(?:line|lines|production line|production lines)\s+{word}\b", normalized):
            mentions.append(number)
    return sorted(set(mentions))


def _detected_goals(text: str) -> list[str]:
    normalized = _normalize_text(text)
    goals: list[str] = []
    for goal, patterns in GOAL_PATTERNS.items():
        if any(re.search(pattern, normalized) for pattern in patterns):
            goals.append(goal)
    return goals


def _mentions_supported_instrument(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(term in normalized for terms in SUPPORTED_INSTRUMENT_TERMS.values() for term in terms)


def _mentions_kpi_update(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(term in normalized for term in KPI_TERMS)


def _mentions_priority_update(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(term in normalized for term in PRIORITY_TERMS)


def _mentions_tooling_or_instrument_update(text: str) -> bool:
    normalized = _normalize_text(text)
    has_scope_action = any(term in normalized for term in INSTRUMENT_SCOPE_TERMS)
    has_tool_term = any(term in normalized for term in TOOLING_POLICY_TERMS) or _mentions_supported_instrument(text)
    return has_scope_action and has_tool_term


def _mentions_simulation_config_update(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(term in normalized for term in SIMULATION_CONFIG_TERMS)


def _mentions_manipulator_priority_update(text: str) -> bool:
    normalized = _normalize_text(text)
    if "prioritize" in normalized and "focus" in normalized and "ent" in normalized:
        return True
    has_pick_language = any(term in normalized for term in MANIPULATOR_PRIORITY_TERMS)
    has_order_language = any(term in normalized for term in ("first", "last", "before", "after", "order", "prioritize"))
    return has_pick_language and has_order_language


def _ambiguous_priority_adjustment(text: str) -> bool:
    normalized = _normalize_text(text)
    if "prioritize" not in normalized:
        return False
    if not any(term in normalized for term in ("adjustment", "adjustments", "adjust")):
        return False
    disambiguating_terms = (
        "pick",
        "grasp",
        "required",
        "wanted",
        "unwanted",
        "focus on",
        "ent surgical tooling set",
        "ent tooling set",
        "ent set",
    )
    return not any(term in normalized for term in disambiguating_terms)


def deterministic_intent_precheck(intent_text: str, current_trt: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_text(intent_text)
    current_lines = set(current_trt.get("lines", {}))
    valid_line_numbers = {int(line_id.split("_", 1)[1]) for line_id in current_lines if line_id.startswith("line_")}
    line_numbers = _line_mentions(intent_text)
    line_ids = {f"line_{number}" for number in line_numbers}
    goals = _detected_goals(intent_text)
    detected_request_types: list[str] = []
    unsupported_terms: list[str] = []
    clarification_questions: list[str] = []
    restricted_messages: list[str] = []

    for term, message in RESTRICTED_SIMULATION_SETTINGS.items():
        if term in normalized:
            detected_request_types.append("restricted_simulation_setting")
            unsupported_terms.append(term)
            restricted_messages.append(message)

    is_simulation_config_update = _mentions_simulation_config_update(intent_text)
    is_manipulator_priority_update = _mentions_manipulator_priority_update(intent_text)
    is_ambiguous_priority_adjustment = _ambiguous_priority_adjustment(intent_text)

    if is_ambiguous_priority_adjustment:
        detected_request_types.append("ambiguous_priority_request")
        clarification_questions.append(
            "Do you mean production-line priority, or should the robots on lines 1 and 3 pick ENT-required tools first?"
        )

    if not (is_simulation_config_update or is_manipulator_priority_update or is_ambiguous_priority_adjustment) and (any(term in normalized for term in ALL_LINE_TERMS) or len(line_numbers) > 1):
        detected_request_types.append("multi_line_request")
        clarification_questions.append("Please specify exactly one production line.")

    if is_manipulator_priority_update and not line_numbers and not any(term in normalized for term in ALL_LINE_TERMS):
        detected_request_types.append("missing_line_or_scope")
        clarification_questions.append("Which production line should use this grasp order, or should it apply to all lines?")
    elif not (is_simulation_config_update or is_ambiguous_priority_adjustment) and not line_numbers:
        detected_request_types.append("missing_line")
        clarification_questions.append("Which line should be changed?")
    elif any(number not in valid_line_numbers for number in line_numbers) or any(line_id not in current_lines for line_id in line_ids):
        detected_request_types.append("invalid_line")
        unsupported_terms.extend(sorted(line_ids - current_lines))

    non_goal_update = (
        _mentions_kpi_update(intent_text)
        or _mentions_priority_update(intent_text)
        or _mentions_tooling_or_instrument_update(intent_text)
        or is_simulation_config_update
        or is_manipulator_priority_update
    )
    if _mentions_kpi_update(intent_text):
        detected_request_types.append("KPI_LIMIT_UPDATE")
    if _mentions_priority_update(intent_text):
        detected_request_types.append("PRIORITY_UPDATE")
    if _mentions_tooling_or_instrument_update(intent_text):
        detected_request_types.append("INSTRUMENT_SCOPE_UPDATE")
    if is_simulation_config_update:
        detected_request_types.append("SIMULATION_CONFIG_UPDATE")
    if is_manipulator_priority_update:
        detected_request_types.append("MANIPULATOR_PRIORITY_UPDATE")

    if not goals and not non_goal_update and not is_ambiguous_priority_adjustment:
        detected_request_types.append("missing_goal")
        clarification_questions.append("Which goal should be applied?")
    elif len(goals) > 1:
        detected_request_types.append("conflicting_goal")
        clarification_questions.append("Please choose one goal only.")

    unsupported_instruments = [term for term in UNSUPPORTED_INSTRUMENT_TERMS if term in normalized]
    if unsupported_instruments:
        detected_request_types.append("unsupported_instrument")
        unsupported_terms.extend(unsupported_instruments)

    if any(term in normalized for term in READONLY_STATE_TERMS) and not any(goal_term in normalized for goal_term in ("routine", "trauma", "backlog")):
        detected_request_types.append("read_only_state_request")
        clarification_questions.append("State fields are read-only; provide an editable task requirement instead.")

    if "exclude" in normalized and not _mentions_supported_instrument(intent_text) and not unsupported_instruments:
        detected_request_types.append("unsupported_instrument")
        clarification_questions.append("Which supported instrument should be excluded?")

    blocking_request_types = {
        "conflicting_goal",
        "invalid_line",
        "missing_goal",
        "missing_line",
        "missing_line_or_scope",
        "multi_line_request",
        "read_only_state_request",
        "restricted_simulation_setting",
        "unsupported_instrument",
        "ambiguous_priority_request",
    }

    action = "PROPOSE_PATCH"
    if any(kind in detected_request_types for kind in ("invalid_line", "unsupported_instrument", "read_only_state_request", "restricted_simulation_setting")):
        action = "UNSUPPORTED_REQUEST"
    elif any(kind in blocking_request_types for kind in detected_request_types):
        action = "NEEDS_CLARIFICATION"

    return {
        "action": action,
        "detected_request_types": sorted(set(detected_request_types)),
        "clarification_questions": [*clarification_questions, *sorted(set(restricted_messages))],
        "unsupported_terms": sorted(set(unsupported_terms)),
    }
