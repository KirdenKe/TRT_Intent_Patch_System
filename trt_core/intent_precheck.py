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
UNSUPPORTED_INSTRUMENT_TERMS = ["laser scalpel", "scalpel", "stapler", "staplers"]
READONLY_STATE_TERMS = ["pause", "paused", "running", "error mode", "last exception", "wip", "state", "mode"]
ALL_LINE_TERMS = ["every line", "all lines", "each line", "line 1 and line 2", "lines 1 and 2", "line one and line two"]
SPELLED_LINES = {"one": 1, "two": 2, "three": 3, "four": 4, "nine": 9}


def _normalize_text(text: str) -> str:
    return text.lower().replace("-", " ")


def _line_mentions(text: str) -> list[int]:
    normalized = _normalize_text(text)
    mentions = [int(match) for match in re.findall(r"\bline\s+(\d+)\b", normalized)]
    for word, number in SPELLED_LINES.items():
        if re.search(rf"\bline\s+{word}\b", normalized):
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

    if any(term in normalized for term in ALL_LINE_TERMS) or len(line_numbers) > 1:
        detected_request_types.append("multi_line_request")
        clarification_questions.append("Please specify exactly one production line.")

    if not line_numbers:
        detected_request_types.append("missing_line")
        clarification_questions.append("Which line should be changed?")
    elif any(number not in valid_line_numbers for number in line_numbers) or any(line_id not in current_lines for line_id in line_ids):
        detected_request_types.append("invalid_line")
        unsupported_terms.extend(sorted(line_ids - current_lines))

    if not goals:
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

    action = "PROPOSE_PATCH"
    if any(kind in detected_request_types for kind in ("invalid_line", "unsupported_instrument", "read_only_state_request")):
        action = "UNSUPPORTED_REQUEST"
    elif detected_request_types:
        action = "NEEDS_CLARIFICATION"

    return {
        "action": action,
        "detected_request_types": sorted(set(detected_request_types)),
        "clarification_questions": clarification_questions,
        "unsupported_terms": sorted(set(unsupported_terms)),
    }

