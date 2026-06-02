"""Normalize domain-level LLM candidates into deterministic Intent Patches."""

from __future__ import annotations

import logging
from typing import Any

from jsonschema import Draft202012Validator


logger = logging.getLogger(__name__)

REQUEST_TYPES = [
    "TASK_GOAL_UPDATE",
    "INSTRUMENT_SCOPE_UPDATE",
    "KPI_LIMIT_UPDATE",
    "PRIORITY_UPDATE",
    "ABNORMAL_STRATEGY_UPDATE",
    "TOOLING_POLICY_UPDATE",
    "MULTI_LINE_POLICY_UPDATE",
    "single_line_patch",
    "multi_line_request",
    "missing_line",
    "missing_goal",
    "invalid_line",
    "unsupported_instrument",
    "read_only_state_request",
    "conflicting_goal",
]

SUPPORTED_INSTRUMENTS = ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"]
TOOLING_REQUIRED_SCOPES = ["ALLOWED_INSTRUMENTS", "ALL_SUPPORTED_INSTRUMENTS", "SELECTED_TOOLING", "NONE"]


LLM_EXTRACTED_FIELDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["PROPOSE_PATCH", "NEEDS_CLARIFICATION", "UNSUPPORTED_REQUEST"]},
        "line_id": {"type": ["string", "null"], "enum": ["line_1", "line_2", "line_3", "line_4", None]},
        "target_scope": {"type": ["string", "null"], "enum": ["SINGLE_LINE", "MULTIPLE_LINES", "ALL_LINES", None]},
        "target_lines": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": ["line_1", "line_2", "line_3", "line_4"]},
        },
        "goal": {
            "type": ["string", "null"],
            "enum": ["ROUTINE_CLASSIFICATION", "TRAUMA_SET_PRIORITY", "BACKLOG_CLEARING", None],
        },
        "priority": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
        "allowed_instruments": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": SUPPORTED_INSTRUMENTS},
        },
        "excluded_instruments": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": SUPPORTED_INSTRUMENTS},
        },
        "kpi_updates": {
            "type": ["object", "null"],
            "properties": {
                "deadline_minutes": {"type": ["integer", "null"]},
                "max_downtime_seconds": {"type": ["integer", "null"]},
                "min_throughput_per_hour": {"type": ["integer", "null"]},
            },
            "additionalProperties": False,
        },
        "tooling_policy": {
            "type": ["object", "null"],
            "required": ["required_scope"],
            "properties": {
                "required_scope": {"type": "string", "enum": TOOLING_REQUIRED_SCOPES},
            },
            "additionalProperties": False,
        },
        "abnormal_strategy": {
            "type": ["string", "null"],
            "enum": ["STOP_LINE", "CONTINUE_FEASIBLE_TASKS", "ASK_OPERATOR", None],
        },
        "clarification_questions": {"type": "array", "items": {"type": "string"}},
        "unsupported_terms": {"type": "array", "items": {"type": "string"}},
        "detected_request_types": {
            "type": "array",
            "items": {"type": "string", "enum": REQUEST_TYPES},
        },
        "request_types": {
            "type": "array",
            "items": {"type": "string", "enum": REQUEST_TYPES},
        },
    },
    "required": [
        "action",
        "line_id",
        "target_scope",
        "target_lines",
        "goal",
        "priority",
        "allowed_instruments",
        "excluded_instruments",
        "kpi_updates",
        "tooling_policy",
        "abnormal_strategy",
        "clarification_questions",
        "unsupported_terms",
        "detected_request_types",
        "request_types",
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
        "line_id": {"type": ["string", "null"], "enum": ["line_1", "line_2", "line_3", "line_4", None]},
        "action": {"type": ["string", "null"], "enum": ["PROPOSE_PATCH", "NEEDS_CLARIFICATION", "UNSUPPORTED_REQUEST", None]},
        "target_scope": {"type": ["string", "null"], "enum": ["SINGLE_LINE", "MULTIPLE_LINES", "ALL_LINES", None]},
        "target_lines": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": ["line_1", "line_2", "line_3", "line_4"]},
        },
        "request_types": {"type": ["array", "null"], "items": {"type": "string", "enum": REQUEST_TYPES}},
        "goal": {
            "type": ["string", "null"],
            "enum": ["ROUTINE_CLASSIFICATION", "TRAUMA_SET_PRIORITY", "BACKLOG_CLEARING", None],
        },
        "priority": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
        "allowed_instruments": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": SUPPORTED_INSTRUMENTS},
        },
        "excluded_instruments": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": SUPPORTED_INSTRUMENTS},
        },
        "kpi_updates": {
            "type": ["object", "null"],
            "properties": {
                "deadline_minutes": {"type": ["integer", "null"]},
                "max_downtime_seconds": {"type": ["integer", "null"]},
                "min_throughput_per_hour": {"type": ["integer", "null"]},
            },
            "additionalProperties": False,
        },
        "tooling_policy": {
            "type": ["object", "null"],
            "required": ["required_scope"],
            "properties": {
                "required_scope": {"type": "string", "enum": TOOLING_REQUIRED_SCOPES},
            },
            "additionalProperties": False,
        },
        "abnormal_strategy": {
            "type": ["string", "null"],
            "enum": ["STOP_LINE", "CONTINUE_FEASIBLE_TASKS", "ASK_OPERATOR", None],
        },
        "clarification_questions": {"type": ["array", "null"], "items": {"type": "string"}},
        "unsupported_terms": {"type": ["array", "null"], "items": {"type": "string"}},
        "detected_request_types": {"type": ["array", "null"], "items": {"type": "string", "enum": REQUEST_TYPES}},
        "status": {"type": "string", "enum": ["DRAFT", "REVIEWED"]},
    },
    "required": [
        "patch_id",
        "trt_id",
        "base_version",
        "operator_id",
        "intent_text",
        "reason",
        "excluded_instruments",
        "status",
    ],
    "additionalProperties": False,
}


def validate_domain_candidate(candidate: dict[str, Any], current_trt: dict[str, Any]) -> list[str]:
    candidate = _coerce_domain_candidate_v2(candidate, current_trt)
    validator = Draft202012Validator(DOMAIN_CANDIDATE_SCHEMA)
    reasons = _dedupe_reasons(error.message for error in sorted(validator.iter_errors(candidate), key=lambda item: item.path))

    if candidate.get("trt_id") != current_trt.get("trt_id"):
        reasons.append(f"trt_id mismatch: candidate targets {candidate.get('trt_id')}, current is {current_trt.get('trt_id')}")
    if candidate.get("base_version") != current_trt.get("version"):
        reasons.append(
            f"base_version mismatch: candidate targets {candidate.get('base_version')}, current is {current_trt.get('version')}"
        )
    if candidate.get("line_id") and candidate.get("line_id") not in current_trt.get("lines", {}):
        reasons.append(f"line_id not found in current TRT: {candidate.get('line_id')}")
    for line_id in candidate.get("target_lines", []):
        if line_id not in current_trt.get("lines", {}):
            reasons.append(f"target line not found in current TRT: {line_id}")
    if not reasons:
        reasons.extend(_tooling_policy_consistency_reasons(candidate))
    if not reasons:
        reasons.extend(_multi_line_error_mode_reasons(candidate, current_trt))

    return _dedupe_reasons(reasons)


def normalize_domain_candidate(candidate: dict[str, Any], current_trt: dict[str, Any]) -> dict[str, Any]:
    candidate = _coerce_domain_candidate_v2(candidate, current_trt)
    logger.info("coerced_candidate.tooling_policy=%r", candidate.get("tooling_policy"))
    reasons = validate_domain_candidate(candidate, current_trt)
    if reasons:
        raise ValueError("; ".join(reasons))

    line_ids = _target_lines(candidate, current_trt)
    operations: list[dict[str, Any]] = []
    for line_id in line_ids:
        tooling_policy = candidate.get("tooling_policy")
        if tooling_policy:
            required_scope = tooling_policy.get("required_scope")
            if "tooling_policy" in current_trt["lines"][line_id]:
                operations.append(
                    {
                        "op": "replace",
                        "path": f"/lines/{line_id}/tooling_policy/required_scope",
                        "value": required_scope,
                    }
                )
            else:
                operations.append(
                    {
                        "op": "add",
                        "path": f"/lines/{line_id}/tooling_policy",
                        "value": {"required_scope": required_scope},
                    }
                )
        if candidate.get("goal") is not None:
            operations.append({"op": "replace", "path": f"/lines/{line_id}/goal", "value": candidate["goal"]})
        if candidate.get("priority") is not None:
            operations.append({"op": "replace", "path": f"/lines/{line_id}/priority", "value": candidate["priority"]})
        if candidate.get("allowed_instruments") is not None:
            operations.append(
                {
                    "op": "replace",
                    "path": f"/lines/{line_id}/allowed_instruments",
                    "value": candidate["allowed_instruments"],
                }
            )
        if candidate.get("excluded_instruments") is not None:
            operations.append(
                {
                    "op": "replace",
                    "path": f"/lines/{line_id}/excluded_instruments",
                    "value": candidate["excluded_instruments"],
                }
            )
        for field, value in (candidate.get("kpi_updates") or {}).items():
            operations.append({"op": "replace", "path": f"/lines/{line_id}/kpi/{field}", "value": value})
        if candidate.get("abnormal_strategy") is not None:
            operations.append(
                {"op": "replace", "path": f"/lines/{line_id}/abnormal_strategy", "value": candidate["abnormal_strategy"]}
            )
    return {
        "patch_id": candidate["patch_id"],
        "trt_id": candidate["trt_id"],
        "base_version": candidate["base_version"],
        "operator_id": candidate["operator_id"],
        "intent_text": candidate["intent_text"],
        "reason": candidate["reason"],
        "operations": operations,
        "status": "REVIEWED",
    }


def _target_lines(candidate: dict[str, Any], current_trt: dict[str, Any]) -> list[str]:
    if candidate.get("target_scope") == "ALL_LINES":
        return sorted(current_trt["lines"])
    if candidate.get("target_lines"):
        return list(candidate["target_lines"])
    if candidate.get("line_id"):
        return [candidate["line_id"]]
    raise ValueError("target_lines or line_id is required unless target_scope is ALL_LINES.")


def _coerce_domain_candidate_v2(candidate: dict[str, Any], current_trt: dict[str, Any]) -> dict[str, Any]:
    coerced = dict(candidate)
    tooling_policy = _coerce_tooling_policy(coerced)
    if tooling_policy is not None:
        coerced["tooling_policy"] = tooling_policy
    if tooling_policy and tooling_policy.get("required_scope") == "ALL_SUPPORTED_INSTRUMENTS":
        if coerced.get("allowed_instruments") is None:
            coerced["allowed_instruments"] = list(SUPPORTED_INSTRUMENTS)
        if coerced.get("excluded_instruments") is None:
            coerced["excluded_instruments"] = []
    if coerced.get("target_scope") is None and coerced.get("line_id"):
        coerced["target_scope"] = "SINGLE_LINE"
    if coerced.get("target_lines") is None and coerced.get("line_id"):
        coerced["target_lines"] = [coerced["line_id"]]
    if coerced.get("target_scope") == "ALL_LINES" and coerced.get("target_lines") is None:
        coerced["target_lines"] = []
    if coerced.get("request_types") is None and coerced.get("detected_request_types") is not None:
        coerced["request_types"] = list(coerced.get("detected_request_types") or [])
    if coerced.get("request_types") is None:
        coerced["request_types"] = _infer_request_types(coerced)
    if coerced.get("detected_request_types") is None:
        coerced["detected_request_types"] = list(coerced.get("request_types") or [])
    return coerced


def _coerce_tooling_policy(candidate: dict[str, Any]) -> dict[str, Any] | None:
    tooling_policy = candidate.get("tooling_policy")
    if not tooling_policy:
        return None
    if tooling_policy.get("required_scope") in TOOLING_REQUIRED_SCOPES:
        return {"required_scope": tooling_policy["required_scope"]}

    intent_text = str(candidate.get("intent_text") or "").lower()
    if "all supported instruments" in intent_text:
        return {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}
    if (
        "all tooling required by each production line" in intent_text
        or "all tooling required by every production line" in intent_text
        or "all tooling required by all production lines" in intent_text
        or "all tooling required" in intent_text
    ):
        return {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}

    if tooling_policy.get("all_required") is True:
        return {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}
    if tooling_policy.get("all_required") is False:
        return {"required_scope": "NONE"}
    return dict(tooling_policy)


def _multi_line_error_mode_reasons(candidate: dict[str, Any], current_trt: dict[str, Any]) -> list[str]:
    request_types = set(candidate.get("request_types") or [])
    if candidate.get("target_scope") != "ALL_LINES":
        return []
    if not (
        request_types & {"MULTI_LINE_POLICY_UPDATE", "TOOLING_POLICY_UPDATE", "KPI_LIMIT_UPDATE", "PRIORITY_UPDATE"}
        or candidate.get("tooling_policy")
        or candidate.get("kpi_updates")
    ):
        return []

    reasons = []
    for line_id in _target_lines(candidate, current_trt):
        mode = current_trt.get("lines", {}).get(line_id, {}).get("state", {}).get("mode")
        if mode == "ERROR":
            reasons.append(
                f"{line_id} is currently in ERROR mode. "
                "Resolve the line error or confirm that ERROR lines should be excluded."
            )
    return reasons


def _tooling_policy_consistency_reasons(candidate: dict[str, Any]) -> list[str]:
    tooling_policy = candidate.get("tooling_policy") or {}
    if tooling_policy.get("required_scope") != "ALL_SUPPORTED_INSTRUMENTS":
        return []
    if candidate.get("allowed_instruments") == []:
        return [
            "tooling_policy.required_scope=ALL_SUPPORTED_INSTRUMENTS requires "
            "allowed_instruments to contain SCISSORS, FORCEPS, CLAMPS, and RETRACTOR."
        ]
    return []


def _infer_request_types(candidate: dict[str, Any]) -> list[str]:
    request_types: list[str] = []
    if candidate.get("goal") is not None:
        request_types.append("TASK_GOAL_UPDATE")
    if candidate.get("priority") is not None:
        request_types.append("PRIORITY_UPDATE")
    if candidate.get("allowed_instruments") is not None or candidate.get("excluded_instruments") is not None:
        request_types.append("INSTRUMENT_SCOPE_UPDATE")
    if candidate.get("abnormal_strategy") is not None:
        request_types.append("ABNORMAL_STRATEGY_UPDATE")
    if candidate.get("kpi_updates"):
        request_types.append("KPI_LIMIT_UPDATE")
    if candidate.get("tooling_policy"):
        request_types.append("TOOLING_POLICY_UPDATE")
    if candidate.get("target_scope") in {"MULTIPLE_LINES", "ALL_LINES"}:
        request_types.append("MULTI_LINE_POLICY_UPDATE")
    return list(dict.fromkeys(request_types))


def _dedupe_reasons(reasons: Any) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason not in seen:
            deduped.append(reason)
            seen.add(reason)
    return deduped
