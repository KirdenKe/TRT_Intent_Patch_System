"""Normalize domain-level LLM candidates into deterministic Intent Patches."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator


LLM_EXTRACTED_FIELDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["PROPOSE_PATCH", "NEEDS_CLARIFICATION", "UNSUPPORTED_REQUEST"]},
        "line_id": {"type": ["string", "null"], "enum": ["line_1", "line_2", "line_3", "line_4", None]},
        "goal": {
            "type": ["string", "null"],
            "enum": ["ROUTINE_CLASSIFICATION", "TRAUMA_SET_PRIORITY", "BACKLOG_CLEARING", None],
        },
        "excluded_instruments": {
            "type": "array",
            "items": {"type": "string", "enum": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"]},
        },
        "clarification_questions": {"type": "array", "items": {"type": "string"}},
        "unsupported_terms": {"type": "array", "items": {"type": "string"}},
        "detected_request_types": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "single_line_patch",
                    "multi_line_request",
                    "missing_line",
                    "missing_goal",
                    "invalid_line",
                    "unsupported_instrument",
                    "read_only_state_request",
                    "conflicting_goal",
                ],
            },
        },
    },
    "required": [
        "action",
        "line_id",
        "goal",
        "excluded_instruments",
        "clarification_questions",
        "unsupported_terms",
        "detected_request_types",
    ],
    "additionalProperties": False,
}


DOMAIN_CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patch_id": {"type": "string"},
        "trt_id": {"type": "string"},
        "base_version": {"type": "string"},
        "operator_id": {"type": "string"},
        "intent_text": {"type": "string"},
        "reason": {"type": "string"},
        "line_id": {"type": "string", "enum": ["line_1", "line_2", "line_3", "line_4"]},
        "goal": {"type": "string", "enum": ["ROUTINE_CLASSIFICATION", "TRAUMA_SET_PRIORITY", "BACKLOG_CLEARING"]},
        "excluded_instruments": {
            "type": "array",
            "items": {"type": "string", "enum": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"]},
        },
        "status": {"type": "string", "enum": ["DRAFT", "REVIEWED"]},
    },
    "required": [
        "patch_id",
        "trt_id",
        "base_version",
        "operator_id",
        "intent_text",
        "reason",
        "line_id",
        "goal",
        "excluded_instruments",
        "status",
    ],
    "additionalProperties": False,
}


def validate_domain_candidate(candidate: dict[str, Any], current_trt: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(DOMAIN_CANDIDATE_SCHEMA)
    reasons = [error.message for error in sorted(validator.iter_errors(candidate), key=lambda item: item.path)]

    if candidate.get("trt_id") != current_trt.get("trt_id"):
        reasons.append(f"trt_id mismatch: candidate targets {candidate.get('trt_id')}, current is {current_trt.get('trt_id')}")
    if candidate.get("base_version") != current_trt.get("version"):
        reasons.append(
            f"base_version mismatch: candidate targets {candidate.get('base_version')}, current is {current_trt.get('version')}"
        )
    if candidate.get("line_id") not in current_trt.get("lines", {}):
        reasons.append(f"line_id not found in current TRT: {candidate.get('line_id')}")

    return reasons


def normalize_domain_candidate(candidate: dict[str, Any], current_trt: dict[str, Any]) -> dict[str, Any]:
    reasons = validate_domain_candidate(candidate, current_trt)
    if reasons:
        raise ValueError("; ".join(reasons))

    line_id = candidate["line_id"]
    return {
        "patch_id": candidate["patch_id"],
        "trt_id": candidate["trt_id"],
        "base_version": candidate["base_version"],
        "operator_id": candidate["operator_id"],
        "intent_text": candidate["intent_text"],
        "reason": candidate["reason"],
        "operations": [
            {"op": "replace", "path": f"/lines/{line_id}/goal", "value": candidate["goal"]},
            {
                "op": "replace",
                "path": f"/lines/{line_id}/excluded_instruments",
                "value": candidate["excluded_instruments"],
            },
        ],
        "status": "REVIEWED",
    }
