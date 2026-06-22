"""Normalize domain-level LLM candidates into deterministic Intent Patches."""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator


logger = logging.getLogger(__name__)

REQUEST_TYPES = [
    "TASK_GOAL_UPDATE",
    "INSTRUMENT_SCOPE_UPDATE",
    "KPI_LIMIT_UPDATE",
    "PRIORITY_UPDATE",
    "MANIPULATOR_PRIORITY_UPDATE",
    "ABNORMAL_STRATEGY_UPDATE",
    "TOOLING_POLICY_UPDATE",
    "SIMULATION_CONFIG_UPDATE",
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
SUPPORTED_TOOL_IDS = [f"tool_{index:02d}" for index in range(1, 28)]
DEFAULT_TOOL_TYPE_ALIASES = {
    "forceps": "FORCEPS",
    "scissor": "SCISSORS",
    "scissors": "SCISSORS",
    "double ended surgical retractor": "DOUBLE_ENDED_SURGICAL_RETRACTOR",
    "double-ended surgical retractor": "DOUBLE_ENDED_SURGICAL_RETRACTOR",
    "surgical forceps": "SURGICAL_FORCEPS",
    "knife handle": "KNIFE_HANDLE",
    "knife handles": "KNIFE_HANDLE",
    "sponge forceps": "SPONGE_FORCEPS",
    "needle holder": "NEEDLE_HOLDER",
    "needle holders": "NEEDLE_HOLDER",
    "nerve retractor": "NERVE_RETRACTOR",
    "mastoid retractor": "MASTOID_RETRACTOR",
    "surgical suction cannula": "SURGICAL_SUCTION_CANNULA",
    "retractor": "RETRACTOR",
}
DEFAULT_TARGET_SET_ALIASES = {
    "ent surgical tooling set": "ENT_SURGICAL_TOOLING_SET",
    "ent tooling set": "ENT_SURGICAL_TOOLING_SET",
    "ent surgical set": "ENT_SURGICAL_TOOLING_SET",
    "ent set": "ENT_SURGICAL_TOOLING_SET",
}
TOOLING_REQUIRED_SCOPES = [
    "ALLOWED_INSTRUMENTS",
    "ALL_SUPPORTED_INSTRUMENTS",
    "ALL_SUPPORTED_TOOLING",
    "SELECTED_TOOLING",
    "NONE",
]
MANIPULATOR_PRIORITY_POLICIES = [
    "FCFS",
    "REQUIRED_FIRST",
    "UNWANTED_FIRST",
    "EXPLICIT_TOOL_ORDER",
    "EXPLICIT_TYPE_ORDER",
    "HIGHEST_RISK_FIRST",
    "LOWEST_RISK_FIRST",
]
IMPLEMENTED_MANIPULATOR_PRIORITY_POLICIES = [
    "FCFS",
    "REQUIRED_FIRST",
    "UNWANTED_FIRST",
    "EXPLICIT_TOOL_ORDER",
    "EXPLICIT_TYPE_ORDER",
]
RESTRICTED_SIMULATION_SETTING_MESSAGES = {
    "layout_source": "layout_source is an infrastructure simulation setting and cannot be changed through normal operator requests.",
    "layout source": "layout_source is an infrastructure simulation setting and cannot be changed through normal operator requests.",
    "max_seed_trials": "max_seed_trials is an internal developer sweep parameter and cannot be changed through normal operator requests.",
    "max seed trials": "max_seed_trials is an internal developer sweep parameter and cannot be changed through normal operator requests.",
    "seed_db_path": "seed_db_path is infrastructure configuration and cannot be changed through normal operator requests.",
    "seed db path": "seed_db_path is infrastructure configuration and cannot be changed through normal operator requests.",
    "reuse_precomputed_layouts": "reuse_precomputed_layouts is an internal layout-cache setting and cannot be changed through normal operator requests.",
    "reuse precomputed layouts": "reuse_precomputed_layouts is an internal layout-cache setting and cannot be changed through normal operator requests.",
}


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
        "selected_normalized_types": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
        "excluded_normalized_types": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
        "selected_tool_ids": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": SUPPORTED_TOOL_IDS},
        },
        "excluded_tool_ids": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": SUPPORTED_TOOL_IDS},
        },
        "required_tool_ids": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": SUPPORTED_TOOL_IDS},
        },
        "target_set_id": {"type": ["string", "null"]},
        "manipulator_priority": {
            "type": ["object", "null"],
            "properties": {
                "policy": {"type": "string", "enum": IMPLEMENTED_MANIPULATOR_PRIORITY_POLICIES},
                "ordered_tool_ids": {
                    "type": "array",
                    "items": {"type": "string", "enum": SUPPORTED_TOOL_IDS},
                },
                "ordered_normalized_types": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "tie_breaker": {"type": "string", "enum": ["FCFS"]},
                "enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "simulation_config_updates": {
            "type": ["object", "null"],
            "properties": {
                "headless": {"type": ["boolean", "null"]},
                "global_seed": {"type": ["integer", "null"], "minimum": 0},
                "reuse_verified_seed": {"type": ["boolean", "null"]},
                "add_reference_number": {"type": ["integer", "null"], "minimum": 0},
                "allowed_overlap_ratio": {"type": ["number", "null"], "minimum": 0},
                "chosen_intervention_mode": {
                    "type": ["string", "null"],
                    "enum": ["continue-until-arrival", "immediate-stop", None],
                },
                "travel_time": {"type": ["number", "null"], "minimum": 0},
                "fix_duration": {"type": ["number", "null"], "minimum": 0},
                "resume_delay": {"type": ["number", "null"], "minimum": 0},
                "episode_success_requires_reset_cycles": {"type": ["integer", "null"], "minimum": 1},
            },
            "additionalProperties": False,
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
        "selected_normalized_types",
        "excluded_normalized_types",
        "selected_tool_ids",
        "excluded_tool_ids",
        "required_tool_ids",
        "target_set_id",
        "manipulator_priority",
        "simulation_config_updates",
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
        "selected_normalized_types": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
        "excluded_normalized_types": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
        "selected_tool_ids": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": SUPPORTED_TOOL_IDS},
        },
        "excluded_tool_ids": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": SUPPORTED_TOOL_IDS},
        },
        "required_tool_ids": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": SUPPORTED_TOOL_IDS},
        },
        "target_set_id": {"type": ["string", "null"]},
        "manipulator_priority": {
            "type": ["object", "null"],
            "properties": {
                "policy": {"type": "string", "enum": IMPLEMENTED_MANIPULATOR_PRIORITY_POLICIES},
                "ordered_tool_ids": {
                    "type": "array",
                    "items": {"type": "string", "enum": SUPPORTED_TOOL_IDS},
                },
                "ordered_normalized_types": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "tie_breaker": {"type": "string", "enum": ["FCFS"]},
                "enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "simulation_config_updates": {
            "type": ["object", "null"],
            "properties": {
                "headless": {"type": ["boolean", "null"]},
                "global_seed": {"type": ["integer", "null"], "minimum": 0},
                "reuse_verified_seed": {"type": ["boolean", "null"]},
                "add_reference_number": {"type": ["integer", "null"], "minimum": 0},
                "allowed_overlap_ratio": {"type": ["number", "null"], "minimum": 0},
                "chosen_intervention_mode": {
                    "type": ["string", "null"],
                    "enum": ["continue-until-arrival", "immediate-stop", None],
                },
                "travel_time": {"type": ["number", "null"], "minimum": 0},
                "fix_duration": {"type": ["number", "null"], "minimum": 0},
                "resume_delay": {"type": ["number", "null"], "minimum": 0},
                "episode_success_requires_reset_cycles": {"type": ["integer", "null"], "minimum": 1},
            },
            "additionalProperties": False,
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
    validator = Draft202012Validator(schema_for_current_trt(DOMAIN_CANDIDATE_SCHEMA, current_trt))
    reasons = _dedupe_reasons(error.message for error in sorted(validator.iter_errors(candidate), key=lambda item: item.path))
    reasons.extend(_restricted_simulation_setting_reasons(candidate))
    if candidate.get("action") == "NEEDS_CLARIFICATION":
        questions = candidate.get("clarification_questions") or ["Intent requires clarification."]
        reasons.extend(str(question) for question in questions)
    if candidate.get("action") == "UNSUPPORTED_REQUEST":
        unsupported = candidate.get("unsupported_terms") or ["Unsupported request."]
        reasons.extend(str(term) for term in unsupported)

    if candidate.get("trt_id") != current_trt.get("trt_id"):
        reasons.append(f"trt_id mismatch: candidate targets {candidate.get('trt_id')}, current is {current_trt.get('trt_id')}")
    if candidate.get("base_version") != current_trt.get("version"):
        reasons.append(
            f"base_version mismatch: candidate targets {candidate.get('base_version')}, current is {current_trt.get('version')}"
        )
    if candidate.get("line_id") and candidate.get("line_id") not in current_trt.get("lines", {}):
        reasons.append(f"line_id not found in current TRT: {candidate.get('line_id')}")
    for line_id in candidate.get("target_lines") or []:
        if line_id not in current_trt.get("lines", {}):
            reasons.append(f"target line not found in current TRT: {line_id}")
    if candidate.get("target_set_id") is not None and candidate["target_set_id"] not in (current_trt.get("tool_sets") or {}):
        reasons.append(f"target_set_id not found in current TRT: {candidate['target_set_id']}")
    manipulator_priority = candidate.get("manipulator_priority") or {}
    if manipulator_priority:
        reasons.extend(_manipulator_priority_reasons(manipulator_priority, current_trt))
    if not reasons:
        reasons.extend(_tooling_policy_consistency_reasons(candidate))
    if not reasons:
        reasons.extend(_multi_line_error_mode_reasons(candidate, current_trt))

    return _dedupe_reasons(reasons)


def schema_for_current_trt(schema: dict[str, Any], current_trt: dict[str, Any]) -> dict[str, Any]:
    scoped = deepcopy(schema)
    line_ids = sorted((current_trt.get("lines") or {}).keys())
    properties = scoped.get("properties", {})
    if line_ids and "line_id" in properties:
        properties["line_id"]["enum"] = [*line_ids, None]
    if line_ids and "target_lines" in properties and isinstance(properties["target_lines"].get("items"), dict):
        properties["target_lines"]["items"]["enum"] = line_ids
    tool_vocabulary = build_tool_vocabulary(current_trt)
    normalized_types = tool_vocabulary["normalized_types"]
    if normalized_types:
        for field in ("allowed_instruments", "excluded_instruments", "selected_normalized_types", "excluded_normalized_types"):
            if field in properties and isinstance(properties[field].get("items"), dict):
                properties[field]["items"]["enum"] = normalized_types
    tool_ids = sorted((current_trt.get("tool_catalog") or {}).keys())
    if tool_ids:
        for field in ("selected_tool_ids", "excluded_tool_ids", "required_tool_ids"):
            if field in properties and isinstance(properties[field].get("items"), dict):
                properties[field]["items"]["enum"] = tool_ids
        manipulator_priority = properties.get("manipulator_priority") or {}
        priority_props = manipulator_priority.get("properties") or {}
        ordered_tool_ids = priority_props.get("ordered_tool_ids") or {}
        if isinstance(ordered_tool_ids.get("items"), dict):
            ordered_tool_ids["items"]["enum"] = tool_ids
    target_set_ids = sorted((current_trt.get("tool_sets") or {}).keys())
    if target_set_ids and "target_set_id" in properties:
        properties["target_set_id"]["enum"] = [*target_set_ids, None]
    if normalized_types:
        manipulator_priority = properties.get("manipulator_priority") or {}
        priority_props = manipulator_priority.get("properties") or {}
        ordered_types = priority_props.get("ordered_normalized_types") or {}
        if isinstance(ordered_types.get("items"), dict):
            ordered_types["items"]["enum"] = normalized_types
    return scoped


def normalize_domain_candidate(candidate: dict[str, Any], current_trt: dict[str, Any]) -> dict[str, Any]:
    candidate = _coerce_domain_candidate_v2(candidate, current_trt)
    logger.info("normalized_candidate=%r", candidate)
    logger.info("normalized_candidate.request_types=%r", candidate.get("request_types"))
    logger.info("normalized_candidate.clarification_questions=%r", candidate.get("clarification_questions"))
    logger.info("normalized_candidate.unsupported_terms=%r", candidate.get("unsupported_terms"))
    logger.info("coerced_candidate.tooling_policy=%r", candidate.get("tooling_policy"))
    reasons = validate_domain_candidate(candidate, current_trt)
    if reasons:
        logger.info("rejection_reasons=%r", reasons)
        raise ValueError("; ".join(reasons))

    line_ids = _target_lines(candidate, current_trt)
    operations: list[dict[str, Any]] = []
    simulation_config_updates = candidate.get("simulation_config_updates") or {}
    operation_driving_fields = (
        "manipulator_priority",
        "tooling_policy",
        "goal",
        "priority",
        "target_set_id",
        "selected_tool_ids",
        "excluded_tool_ids",
        "required_tool_ids",
        "allowed_instruments",
        "excluded_instruments",
        "abnormal_strategy",
    )
    has_operation_driving_update = any(candidate.get(field) is not None for field in operation_driving_fields) or bool(
        candidate.get("kpi_updates")
    )
    if "SIMULATION_CONFIG_UPDATE" in set(candidate.get("request_types") or []) and not has_operation_driving_update:
        if not simulation_config_updates:
            raise ValueError("SIMULATION_CONFIG_UPDATE requires simulation_config_updates.")
        return {
            "patch_id": candidate["patch_id"],
            "trt_id": candidate["trt_id"],
            "base_version": candidate["base_version"],
            "operator_id": candidate["operator_id"],
            "intent_text": candidate["intent_text"],
            "reason": candidate["reason"],
            "operations": [],
            "simulation_config_updates": simulation_config_updates,
            "request_types": candidate.get("request_types") or [],
            "affected_lines": line_ids,
            "message": _simulation_config_review_message(simulation_config_updates),
            "status": "REVIEWED",
        }
    for line_id in line_ids:
        if candidate.get("manipulator_priority") is not None:
            operations.append(
                {
                    "op": "replace" if "manipulator_priority" in current_trt["lines"][line_id] else "add",
                    "path": f"/lines/{line_id}/manipulator_priority",
                    "value": _normalize_manipulator_priority(candidate["manipulator_priority"]),
                }
            )
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
        if candidate.get("target_set_id") is not None:
            operations.append(
                {
                    "op": "replace" if "target_set_id" in current_trt["lines"][line_id] else "add",
                    "path": f"/lines/{line_id}/target_set_id",
                    "value": candidate["target_set_id"],
                }
            )
        if candidate.get("selected_tool_ids") is not None:
            operations.append(
                {
                    "op": "replace" if "selected_tool_ids" in current_trt["lines"][line_id] else "add",
                    "path": f"/lines/{line_id}/selected_tool_ids",
                    "value": candidate["selected_tool_ids"],
                }
            )
        if candidate.get("excluded_tool_ids") is not None:
            existing_excluded = current_trt["lines"][line_id].get("excluded_tool_ids") or []
            excluded_value = _dedupe_values([*existing_excluded, *candidate["excluded_tool_ids"]])
            operations.append(
                {
                    "op": "replace" if "excluded_tool_ids" in current_trt["lines"][line_id] else "add",
                    "path": f"/lines/{line_id}/excluded_tool_ids",
                    "value": excluded_value,
                }
            )
            if current_trt["lines"][line_id].get("selected_tool_ids"):
                selected_value = [
                    tool_id
                    for tool_id in current_trt["lines"][line_id].get("selected_tool_ids", [])
                    if tool_id not in excluded_value
                ]
                operations.append(
                    {
                        "op": "replace",
                        "path": f"/lines/{line_id}/selected_tool_ids",
                        "value": selected_value,
                    }
                )
            if current_trt["lines"][line_id].get("required_tool_ids"):
                required_value = [
                    tool_id
                    for tool_id in current_trt["lines"][line_id].get("required_tool_ids", [])
                    if tool_id not in excluded_value
                ]
                operations.append(
                    {
                        "op": "replace",
                        "path": f"/lines/{line_id}/required_tool_ids",
                        "value": required_value,
                    }
                )
        if candidate.get("required_tool_ids") is not None:
            operations.append(
                {
                    "op": "replace" if "required_tool_ids" in current_trt["lines"][line_id] else "add",
                    "path": f"/lines/{line_id}/required_tool_ids",
                    "value": candidate["required_tool_ids"],
                }
            )
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
    patch = {
        "patch_id": candidate["patch_id"],
        "trt_id": candidate["trt_id"],
        "base_version": candidate["base_version"],
        "operator_id": candidate["operator_id"],
        "intent_text": candidate["intent_text"],
        "reason": candidate["reason"],
        "operations": operations,
        "request_types": candidate.get("request_types") or [],
        "affected_lines": line_ids,
        "status": "REVIEWED",
    }
    if simulation_config_updates:
        patch["simulation_config_updates"] = simulation_config_updates
    if candidate.get("manipulator_priority"):
        patch["message"] = _manipulator_priority_review_message(candidate)
    return patch


def _target_lines(candidate: dict[str, Any], current_trt: dict[str, Any]) -> list[str]:
    if candidate.get("target_scope") == "ALL_LINES":
        return sorted(current_trt["lines"])
    if candidate.get("target_lines"):
        return list(candidate["target_lines"])
    if candidate.get("line_id"):
        return [candidate["line_id"]]
    raise ValueError("target_lines or line_id is required unless target_scope is ALL_LINES.")


def render_target_scope_for_operator(target_scope: str | None, target_lines: list[str] | None) -> str:
    lines = list(target_lines or [])
    if target_scope == "ALL_LINES":
        return "all production lines"
    if target_scope == "MULTIPLE_LINES" and lines:
        numbers = [line.replace("line_", "") for line in lines]
        if len(numbers) == 2:
            return f"lines {numbers[0]} and {numbers[1]}"
        return f"lines {', '.join(numbers[:-1])}, and {numbers[-1]}"
    if target_scope == "SINGLE_LINE" and lines:
        return lines[0].replace("line_", "line ")
    return "the selected production lines"


def _target_scope_from_candidate_or_text(
    candidate: dict[str, Any],
    intent_text: str,
    current_trt: dict[str, Any],
) -> tuple[str | None, list[str]]:
    if _is_all_lines_text(intent_text):
        return "ALL_LINES", []
    parsed_lines = _line_ids_from_text(intent_text, current_trt)
    if parsed_lines:
        return ("MULTIPLE_LINES" if len(parsed_lines) > 1 else "SINGLE_LINE"), parsed_lines
    target_scope = candidate.get("target_scope")
    target_lines = list(candidate.get("target_lines") or ([candidate["line_id"]] if candidate.get("line_id") else []))
    return target_scope, target_lines


def _priority_clarification_question(candidate: dict[str, Any], intent_text: str, current_trt: dict[str, Any]) -> str:
    target_scope, target_lines = _target_scope_from_candidate_or_text(candidate, intent_text, current_trt)
    target_phrase = render_target_scope_for_operator(target_scope, target_lines)
    return f"Do you mean production-line priority, or should the robots on {target_phrase} pick ENT-required tooling first?"


def _is_internal_tooling_count_question(question: str) -> bool:
    normalized = question.lower()
    return (
        "add_reference_number" in normalized
        or "add reference number" in normalized
        or "simulation argument" in normalized
        or ("number of tools" in normalized and "do you mean" in normalized)
    )


def _is_priority_clarification_question(question: str) -> bool:
    normalized = question.lower()
    return "production-line priority" in normalized or "production line priority" in normalized


def _coerce_domain_candidate_v2(candidate: dict[str, Any], current_trt: dict[str, Any]) -> dict[str, Any]:
    coerced = dict(candidate)
    _coerce_simulation_config_intent(coerced)
    _coerce_manipulator_priority_intent(coerced, current_trt)
    _coerce_target_set_intent(coerced, current_trt)
    _coerce_remove_tooling_intent(coerced, current_trt)
    tooling_policy = _coerce_tooling_policy(coerced, current_trt)
    if tooling_policy is not None:
        coerced["tooling_policy"] = tooling_policy
    if tooling_policy and tooling_policy.get("required_scope") == "ALL_SUPPORTED_INSTRUMENTS":
        if coerced.get("allowed_instruments") is None:
            coerced["allowed_instruments"] = list(SUPPORTED_INSTRUMENTS)
        if coerced.get("excluded_instruments") is None:
            coerced["excluded_instruments"] = []
    if tooling_policy and tooling_policy.get("required_scope") == "ALL_SUPPORTED_TOOLING":
        if coerced.get("selected_tool_ids") is None:
            coerced["selected_tool_ids"] = list(SUPPORTED_TOOL_IDS)
        if coerced.get("excluded_tool_ids") is None:
            coerced["excluded_tool_ids"] = []
    if tooling_policy and tooling_policy.get("required_scope") == "NONE" and _has_instance_tooling(current_trt):
        if coerced.get("selected_tool_ids") is None:
            coerced["selected_tool_ids"] = []
        if coerced.get("excluded_tool_ids") is None:
            coerced["excluded_tool_ids"] = []
    if coerced.get("selected_tool_ids") is None and coerced.get("allowed_instruments") is not None:
        selected_tool_ids = _tool_ids_for_normalized_types(coerced["allowed_instruments"], current_trt)
        if selected_tool_ids or _has_instance_tooling(current_trt):
            coerced["selected_tool_ids"] = selected_tool_ids
    if coerced.get("excluded_tool_ids") is None and coerced.get("excluded_instruments") is not None:
        excluded_tool_ids = _tool_ids_for_normalized_types(coerced["excluded_instruments"], current_trt)
        if excluded_tool_ids or _has_instance_tooling(current_trt):
            coerced["excluded_tool_ids"] = excluded_tool_ids
    if coerced.get("selected_tool_ids") is None and coerced.get("selected_normalized_types") is not None:
        coerced["selected_tool_ids"] = _tool_ids_for_normalized_types(coerced["selected_normalized_types"], current_trt)
    if coerced.get("excluded_tool_ids") is None and coerced.get("excluded_normalized_types") is not None:
        coerced["excluded_tool_ids"] = _tool_ids_for_normalized_types(coerced["excluded_normalized_types"], current_trt)
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
    coerced["clarification_questions"] = [
        question
        for question in (coerced.get("clarification_questions") or [])
        if not _is_internal_tooling_count_question(str(question))
    ]
    return coerced


def _coerce_manipulator_priority_intent(candidate: dict[str, Any], current_trt: dict[str, Any]) -> None:
    intent_text = str(candidate.get("intent_text") or "").lower()
    if not intent_text:
        return
    if _is_ambiguous_priority_adjustment(intent_text):
        candidate["action"] = "NEEDS_CLARIFICATION"
        existing_questions = [
            question
            for question in (candidate.get("clarification_questions") or [])
            if not _is_priority_clarification_question(str(question))
            and not _is_internal_tooling_count_question(str(question))
        ]
        candidate["clarification_questions"] = _dedupe_values(
            [
                *existing_questions,
                _priority_clarification_question(candidate, intent_text, current_trt),
            ]
        )
        return
    if not _mentions_grasp_priority(intent_text):
        return

    priority = dict(candidate.get("manipulator_priority") or {})
    if (
        "required" in intent_text
        or "ent-required" in intent_text
        or "ent required" in intent_text
        or "wanted" in intent_text
        or _mentions_target_set_focus(intent_text, current_trt)
    ):
        if "first" in intent_text or "before unwanted" in intent_text:
            priority["policy"] = "REQUIRED_FIRST"
        elif _mentions_target_set_focus(intent_text, current_trt):
            priority["policy"] = "REQUIRED_FIRST"
    if "unwanted" in intent_text and "first" in intent_text:
        priority["policy"] = "UNWANTED_FIRST"

    ordered_tool_ids = _ordered_tool_ids_from_text(intent_text, current_trt)
    if ordered_tool_ids:
        priority["policy"] = "EXPLICIT_TOOL_ORDER"
        priority["ordered_tool_ids"] = ordered_tool_ids

    ordered_types = _ordered_normalized_types_from_text(intent_text, current_trt)
    if ordered_types and not ordered_tool_ids:
        priority["policy"] = "EXPLICIT_TYPE_ORDER"
        priority["ordered_normalized_types"] = ordered_types

    if not priority.get("policy"):
        candidate["clarification_questions"] = _dedupe_values(
            [
                *(candidate.get("clarification_questions") or []),
                "Which production line should use this grasp order, or should it apply to all lines?",
            ]
        )
        candidate["action"] = "NEEDS_CLARIFICATION"
        return

    candidate["goal"] = None
    priority = _normalize_manipulator_priority(priority)
    candidate["manipulator_priority"] = priority
    candidate["request_types"] = _dedupe_values([*(candidate.get("request_types") or []), "MANIPULATOR_PRIORITY_UPDATE"])
    candidate["detected_request_types"] = _dedupe_values(
        [*(candidate.get("detected_request_types") or []), "MANIPULATOR_PRIORITY_UPDATE"]
    )
    if _is_all_lines_text(intent_text):
        candidate["target_scope"] = "ALL_LINES"
        candidate["target_lines"] = []
        candidate["line_id"] = None
    else:
        parsed_lines = _line_ids_from_text(intent_text, current_trt)
        if parsed_lines:
            candidate["target_lines"] = parsed_lines
            candidate["target_scope"] = "MULTIPLE_LINES" if len(parsed_lines) > 1 else "SINGLE_LINE"
            candidate["line_id"] = parsed_lines[0] if len(parsed_lines) == 1 else None
        elif not candidate.get("line_id") and not candidate.get("target_lines") and candidate.get("target_scope") != "ALL_LINES":
            candidate["clarification_questions"] = _dedupe_values(
                [
                    *(candidate.get("clarification_questions") or []),
                    "Which production line should use this grasp order, or should it apply to all lines?",
                ]
            )
            candidate["action"] = "NEEDS_CLARIFICATION"
    candidate["clarification_questions"] = [
        question
        for question in (candidate.get("clarification_questions") or [])
        if "goal" not in question.lower()
    ]


def _mentions_grasp_priority(intent_text: str) -> bool:
    priority_terms = ("pick", "grasp", "pickup", "pick up", "grasp order", "picking order", "priority queue")
    ordering_terms = ("first", "last", "before", "after", "order", "prioritize", "priority")
    if any(term in intent_text for term in priority_terms) and any(term in intent_text for term in ordering_terms):
        return True
    return "prioritize" in intent_text and "focus" in intent_text and "ent" in intent_text


def _mentions_target_set_focus(intent_text: str, current_trt: dict[str, Any]) -> bool:
    if "focus" not in intent_text and "prioritize" not in intent_text:
        return False
    aliases = build_target_set_aliases(current_trt)
    return any(re.search(rf"\b{re.escape(alias.lower())}\b", intent_text) for alias in aliases)


def _is_ambiguous_priority_adjustment(intent_text: str) -> bool:
    if "prioritize" not in intent_text:
        return False
    if not any(term in intent_text for term in ("adjustment", "adjustments", "adjust")):
        return False
    if "focus" in intent_text and "ent" in intent_text:
        return True
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
        "ent-required",
        "ent required",
    )
    return not any(term in intent_text for term in disambiguating_terms)


def _is_all_lines_text(intent_text: str) -> bool:
    return any(
        phrase in intent_text
        for phrase in (
            "all production lines",
            "every production line",
            "each production line",
            "all lines",
            "every line",
            "each line",
            "all robots",
            "every robot",
            "each robot",
        )
    )


def _ordered_tool_ids_from_text(intent_text: str, current_trt: dict[str, Any]) -> list[str]:
    valid = set((current_trt.get("tool_catalog") or {}).keys()) or set(SUPPORTED_TOOL_IDS)
    return _dedupe_values([tool_id for tool_id in re.findall(r"\btool_(?:0[1-9]|1[0-9]|2[0-7])\b", intent_text) if tool_id in valid])


def _ordered_normalized_types_from_text(intent_text: str, current_trt: dict[str, Any]) -> list[str]:
    aliases = build_tool_vocabulary(current_trt)["aliases"]
    matches: list[tuple[int, str]] = []
    for alias, normalized_type in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        for match in re.finditer(rf"\b{re.escape(alias.lower())}\b", intent_text):
            matches.append((match.start(), normalized_type))
    return _dedupe_values(normalized_type for _, normalized_type in sorted(matches))


def _normalize_manipulator_priority(priority: dict[str, Any]) -> dict[str, Any]:
    policy = priority.get("policy") or "FCFS"
    return {
        "policy": policy,
        "ordered_tool_ids": _dedupe_values(priority.get("ordered_tool_ids") or []),
        "ordered_normalized_types": _dedupe_values(priority.get("ordered_normalized_types") or []),
        "tie_breaker": priority.get("tie_breaker") or "FCFS",
        "enabled": bool(priority.get("enabled", True)),
    }


def _manipulator_priority_reasons(priority: dict[str, Any], current_trt: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    normalized = _normalize_manipulator_priority(priority)
    policy = normalized["policy"]
    if policy not in IMPLEMENTED_MANIPULATOR_PRIORITY_POLICIES:
        reasons.append(f"manipulator_priority.policy is not implemented: {policy}")
    if policy == "EXPLICIT_TOOL_ORDER" and not normalized["ordered_tool_ids"]:
        reasons.append("EXPLICIT_TOOL_ORDER requires ordered_tool_ids.")
    if policy == "EXPLICIT_TYPE_ORDER" and not normalized["ordered_normalized_types"]:
        reasons.append("EXPLICIT_TYPE_ORDER requires ordered_normalized_types.")
    valid_tool_ids = set((current_trt.get("tool_catalog") or {}).keys()) or set(SUPPORTED_TOOL_IDS)
    for tool_id in normalized["ordered_tool_ids"]:
        if tool_id not in valid_tool_ids:
            reasons.append(f"ordered_tool_id not found in current TRT: {tool_id}")
    valid_types = set(build_tool_vocabulary(current_trt)["normalized_types"])
    for tool_type in normalized["ordered_normalized_types"]:
        if valid_types and tool_type not in valid_types:
            reasons.append(f"ordered_normalized_type not found in current TRT: {tool_type}")
    return reasons


def _manipulator_priority_review_message(candidate: dict[str, Any]) -> str:
    priority = _normalize_manipulator_priority(candidate.get("manipulator_priority") or {})
    policy = priority["policy"]
    lines = _dedupe_values(candidate.get("target_lines") or [])
    target = render_target_scope_for_operator(
        candidate.get("target_scope"),
        lines or ([candidate["line_id"]] if candidate.get("line_id") else []),
    )
    simulation_updates = candidate.get("simulation_config_updates") or {}
    if policy == "REQUIRED_FIRST" and candidate.get("target_set_id") and simulation_updates.get("add_reference_number") is not None:
        return (
            "The candidate patch passed validation. It will set "
            f"{target} to target the {candidate['target_set_id']}, make their robots pick ENT-required tooling first, "
            f"and set the simulated tooling count to {simulation_updates['add_reference_number']} for the full-system simulation. "
            "Please approve, reject, or request revision."
        )
    if policy == "REQUIRED_FIRST" and candidate.get("target_set_id"):
        return (
            "The candidate patch passed validation. It will set "
            f"{target} to target the {candidate['target_set_id']} and make their robots pick ENT-required tooling first. "
            "Please approve, reject, or request revision."
        )
    if policy == "EXPLICIT_TYPE_ORDER":
        order = ", ".join(priority["ordered_normalized_types"])
        return (
            "The candidate manipulator priority update is valid. It will set "
            f"{target} to pick tooling types in this order: {order}. Please approve, reject, or request revision."
        )
    if policy == "EXPLICIT_TOOL_ORDER":
        order = ", ".join(priority["ordered_tool_ids"])
        return (
            "The candidate manipulator priority update is valid. It will set "
            f"{target} to pick tool IDs in this order: {order}. Please approve, reject, or request revision."
        )
    return (
        "The candidate manipulator priority update is valid. It will set "
        f"{target} manipulator priority policy to {policy}. Please approve, reject, or request revision."
    )


def parse_tooling_count_request(text: str) -> dict[str, int] | None:
    normalized = str(text or "").lower()
    patterns = [
        r"only\s+(\d+)\s+remain",
        r"so that only\s+(\d+)\s+remain",
        r"number of tooling.*?(\d+)",
        r"tooling count.*?(\d+)",
        r"show\s+(\d+)\s+tools",
        r"limit.*?(\d+)\s+tools",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return {"add_reference_number": int(match.group(1))}
    return None


def _coerce_simulation_config_intent(candidate: dict[str, Any]) -> None:
    intent_text = str(candidate.get("intent_text") or "").lower()
    if not intent_text:
        return
    updates = dict(candidate.get("simulation_config_updates") or {})
    if re.search(r"\bheadless\b", intent_text):
        updates["headless"] = not re.search(r"\b(headless\s+false|not\s+headless|disable\s+headless)\b", intent_text)
    if "rendering enabled" in intent_text or "with rendering" in intent_text:
        updates["headless"] = False
    number_patterns = {
        "global_seed": [
            r"\bglobal_seed\b[^\d]*(\d+)\b",
            r"\bglobal seed\b[^\d]*(\d+)\b",
        ],
        "episode_success_requires_reset_cycles": [
            r"\bepisode_success_requires_reset_cycles\b[^\d]*(\d+)\b",
            r"\brequire\s+(\d+)\s+successful\s+reset\s+cycles?\b",
            r"\b(\d+)\s+successful\s+reset\s+cycles?\b",
        ],
        "allowed_overlap_ratio": [
            r"\ballowed_overlap_ratio\b[^\d]*(\d+(?:\.\d+)?)\b",
            r"\ballowed overlap ratio\b[^\d]*(\d+(?:\.\d+)?)\b",
        ],
        "travel_time": [
            r"\btravel_time\b[^\d]*(\d+(?:\.\d+)?)\b",
            r"\boperator travel time\b[^\d]*(\d+(?:\.\d+)?)\b",
            r"\btravel time\b[^\d]*(\d+(?:\.\d+)?)\b",
        ],
        "fix_duration": [
            r"\bfix_duration\b[^\d]*(\d+(?:\.\d+)?)\b",
            r"\bfix duration\b[^\d]*(\d+(?:\.\d+)?)\b",
            r"\bentanglement fix duration\b[^\d]*(\d+(?:\.\d+)?)\b",
        ],
        "resume_delay": [
            r"\bresume_delay\b[^\d]*(\d+(?:\.\d+)?)\b",
            r"\bresume delay\b[^\d]*(\d+(?:\.\d+)?)\b",
        ],
    }
    for field, patterns in number_patterns.items():
        for pattern in patterns:
            match = re.search(pattern, intent_text)
            if match:
                value = float(match.group(1)) if "." in match.group(1) else int(match.group(1))
                updates[field] = value
                break
    tooling_count_update = parse_tooling_count_request(intent_text)
    if tooling_count_update:
        updates.update(tooling_count_update)
    if "add_reference_number" in intent_text or "add reference number" in intent_text:
        number_match = re.search(r"\b(?:add_reference_number|add reference number)\b[^\d]*(\d+)\b", intent_text)
        if not number_match:
            number_match = re.search(r"\bonly\s+(\d+)\s+(?:remain|references?|tooling|tools?)\b", intent_text)
        if number_match:
            updates["add_reference_number"] = int(number_match.group(1))
    elif any(term in intent_text for term in ("number of tooling", "tooling count", "reference tooling")):
        number_match = re.search(r"\bonly\s+(\d+)\s+(?:remain|references?|tooling|tools?)\b", intent_text)
        if number_match:
            updates["add_reference_number"] = int(number_match.group(1))
    if "immediate stop" in intent_text or "immediate-stop" in intent_text:
        updates["chosen_intervention_mode"] = "immediate-stop"
    if (
        "continue until operator arrival" in intent_text
        or "continue until arrival" in intent_text
        or "continue-until-arrival" in intent_text
    ):
        updates["chosen_intervention_mode"] = "continue-until-arrival"
    if updates.get("global_seed") is not None:
        updates["reuse_verified_seed"] = False
    if not updates:
        return
    candidate["simulation_config_updates"] = updates
    candidate["goal"] = None
    if not candidate.get("target_scope") and not candidate.get("target_lines") and not candidate.get("line_id"):
        candidate["line_id"] = None
        candidate["target_scope"] = "ALL_LINES"
        candidate["target_lines"] = []
    candidate["request_types"] = _dedupe_values([*(candidate.get("request_types") or []), "SIMULATION_CONFIG_UPDATE"])
    candidate["detected_request_types"] = _dedupe_values(
        [*(candidate.get("detected_request_types") or []), "SIMULATION_CONFIG_UPDATE"]
    )
    candidate["clarification_questions"] = [
        question
        for question in (candidate.get("clarification_questions") or [])
        if "goal" not in question.lower()
        and "production line" not in question.lower()
        and "specific tool" not in question.lower()
        and "which tool" not in question.lower()
    ]


def _simulation_config_review_message(updates: dict[str, Any]) -> str:
    if updates.get("add_reference_number") is not None:
        return (
            "The candidate simulation configuration update is valid. It will set "
            f"the simulated tooling count to {updates['add_reference_number']} for the full-system simulation."
        )
    fields = ", ".join(sorted(updates))
    return f"The candidate simulation configuration update is valid. It will update {fields} for the simulation."


def _restricted_simulation_setting_reasons(candidate: dict[str, Any]) -> list[str]:
    intent_text = str(candidate.get("intent_text") or "").lower()
    reasons = []
    for term, message in RESTRICTED_SIMULATION_SETTING_MESSAGES.items():
        if term in intent_text:
            reasons.append(message)
    return _dedupe_reasons(reasons)


def _coerce_target_set_intent(candidate: dict[str, Any], current_trt: dict[str, Any]) -> None:
    intent_text = str(candidate.get("intent_text") or "").lower()
    if not intent_text:
        return

    aliases = build_target_set_aliases(current_trt)
    matched_set_id = None
    for alias, set_id in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(alias.lower())}\b", intent_text):
            matched_set_id = set_id
            break
    if not matched_set_id:
        return

    target_words = ("target", "targets", "classify", "classification", "use", "adjust", "set", "focus", "prioritize")
    if not any(word in intent_text for word in target_words):
        return

    candidate["target_set_id"] = candidate.get("target_set_id") or matched_set_id
    explicit_goal_terms = ("routine classification", "trauma", "trauma priority", "trauma set", "backlog", "backlog clearing")
    if not any(term in intent_text for term in explicit_goal_terms):
        candidate["goal"] = None
    candidate["unsupported_terms"] = [
        term
        for term in (candidate.get("unsupported_terms") or [])
        if term.lower() not in aliases
    ]
    candidate["clarification_questions"] = [
        question
        for question in (candidate.get("clarification_questions") or [])
        if "goal" not in question.lower()
    ]
    candidate["request_types"] = _dedupe_values([*(candidate.get("request_types") or []), "TOOLING_POLICY_UPDATE"])
    candidate["detected_request_types"] = _dedupe_values(
        [*(candidate.get("detected_request_types") or []), "TOOLING_POLICY_UPDATE"]
    )
    if any(
        phrase in intent_text
        for phrase in (
            "all production lines",
            "every production line",
            "each production line",
            "all lines",
            "every line",
            "each line",
        )
    ):
        candidate["target_scope"] = "ALL_LINES"
        candidate["target_lines"] = []
        candidate["line_id"] = None
    elif candidate.get("target_lines") is None:
        parsed_lines = _line_ids_from_text(intent_text, current_trt)
        if parsed_lines:
            candidate["target_lines"] = parsed_lines
            candidate["target_scope"] = "MULTIPLE_LINES" if len(parsed_lines) > 1 else "SINGLE_LINE"
            candidate["line_id"] = parsed_lines[0] if len(parsed_lines) == 1 else None


def _coerce_remove_tooling_intent(candidate: dict[str, Any], current_trt: dict[str, Any]) -> None:
    intent_text = str(candidate.get("intent_text") or "").lower()
    if not intent_text:
        return
    remove_terms = ("remove", "exclude", "don't use", "do not use", "take", "out of")
    if not any(term in intent_text for term in remove_terms):
        return

    aliases = build_tool_vocabulary(current_trt)["aliases"]
    matched_types = []
    for alias, normalized_type in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(alias.lower())}\b", intent_text):
            matched_types.append(normalized_type)
    matched_types = _dedupe_values(matched_types)
    if not matched_types:
        return

    candidate.setdefault("goal", None)
    candidate["excluded_normalized_types"] = _dedupe_values([*(candidate.get("excluded_normalized_types") or []), *matched_types])
    if candidate.get("excluded_tool_ids") is None:
        candidate["excluded_tool_ids"] = _tool_ids_for_normalized_types(candidate["excluded_normalized_types"], current_trt)
    if candidate.get("request_types") is None:
        candidate["request_types"] = []
    candidate["request_types"] = _dedupe_values([*(candidate.get("request_types") or []), "INSTRUMENT_SCOPE_UPDATE"])
    if candidate.get("detected_request_types") is None:
        candidate["detected_request_types"] = list(candidate["request_types"])
    if candidate.get("target_lines") is None:
        parsed_lines = _line_ids_from_text(intent_text, current_trt)
        if parsed_lines:
            candidate["target_lines"] = parsed_lines
            candidate["target_scope"] = "MULTIPLE_LINES" if len(parsed_lines) > 1 else "SINGLE_LINE"
            candidate["line_id"] = parsed_lines[0] if len(parsed_lines) == 1 else None


def _line_ids_from_text(intent_text: str, current_trt: dict[str, Any]) -> list[str]:
    valid = set((current_trt.get("lines") or {}).keys())
    found = []
    for match in re.finditer(r"(?:line|lines|production line|production lines)\s+((?:\d+\s*(?:,|and)?\s*)+)", intent_text):
        for number in re.findall(r"\d+", match.group(1)):
            line_id = f"line_{int(number)}"
            if line_id in valid:
                found.append(line_id)
    return _dedupe_values(found)


def _coerce_tooling_policy(candidate: dict[str, Any], current_trt: dict[str, Any]) -> dict[str, Any] | None:
    tooling_policy = candidate.get("tooling_policy")
    if not tooling_policy:
        return None
    if tooling_policy.get("required_scope") in TOOLING_REQUIRED_SCOPES:
        return {"required_scope": tooling_policy["required_scope"]}

    intent_text = str(candidate.get("intent_text") or "").lower()
    if "all supported instruments" in intent_text:
        return {"required_scope": "ALL_SUPPORTED_TOOLING" if _has_instance_tooling(current_trt) else "ALL_SUPPORTED_INSTRUMENTS"}
    if "all supported tooling" in intent_text or "all tooling selected" in intent_text:
        return {"required_scope": "ALL_SUPPORTED_TOOLING" if _has_instance_tooling(current_trt) else "ALL_SUPPORTED_INSTRUMENTS"}
    if (
        "all tooling required by each production line" in intent_text
        or "all tooling required by every production line" in intent_text
        or "all tooling required by all production lines" in intent_text
        or "all tooling required" in intent_text
    ):
        return {"required_scope": "ALL_SUPPORTED_TOOLING" if _has_instance_tooling(current_trt) else "ALL_SUPPORTED_INSTRUMENTS"}

    if tooling_policy.get("all_required") is True:
        return {"required_scope": "ALL_SUPPORTED_TOOLING" if _has_instance_tooling(current_trt) else "ALL_SUPPORTED_INSTRUMENTS"}
    if tooling_policy.get("all_required") is False:
        return {"required_scope": "NONE"}
    return dict(tooling_policy)


def _has_instance_tooling(current_trt: dict[str, Any]) -> bool:
    return bool(current_trt.get("tool_catalog")) or any(
        "selected_tool_ids" in line or "excluded_tool_ids" in line
        for line in (current_trt.get("lines") or {}).values()
    )


def _tool_ids_for_normalized_types(types: list[str], current_trt: dict[str, Any]) -> list[str]:
    if not isinstance(types, list):
        return []
    wanted = set(types)
    catalog = current_trt.get("tool_catalog") or {}
    if not catalog:
        return []
    tool_ids = [
        tool_id
        for tool_id, tool in sorted(catalog.items())
        if tool.get("normalized_type") in wanted
    ]
    if current_trt.get("experiment_id") == "ent_surgical_tooling_sorting_demo" and "KNIFE_HANDLE" in wanted:
        # The operator-facing ENT knife-handle group is the surgical set pair
        # plus the adjacent non-member reference, not the legacy pre-set handle.
        tool_ids = [tool_id for tool_id in tool_ids if tool_id != "tool_05"]
    return tool_ids


def build_tool_vocabulary(current_trt: dict[str, Any]) -> dict[str, Any]:
    catalog = current_trt.get("tool_catalog") or {}
    normalized_types = sorted(
        {
            str(tool.get("normalized_type"))
            for tool in catalog.values()
            if isinstance(tool, dict) and tool.get("normalized_type")
        }
    )
    aliases = dict(DEFAULT_TOOL_TYPE_ALIASES)
    for tool in catalog.values():
        if not isinstance(tool, dict) or not tool.get("normalized_type"):
            continue
        normalized_type = str(tool["normalized_type"])
        type_name = str(tool.get("type") or normalized_type).strip().lower()
        if type_name:
            aliases[type_name] = normalized_type
            if not type_name.endswith("s"):
                aliases[f"{type_name}s"] = normalized_type
    aliases = {alias: value for alias, value in sorted(aliases.items()) if value in normalized_types or value == "RETRACTOR"}
    return {"normalized_types": normalized_types, "aliases": aliases}


def build_target_set_aliases(current_trt: dict[str, Any]) -> dict[str, str]:
    set_ids = set((current_trt.get("tool_sets") or {}).keys())
    aliases = {alias: set_id for alias, set_id in DEFAULT_TARGET_SET_ALIASES.items() if set_id in set_ids}
    for set_id in sorted(set_ids):
        aliases[set_id.lower().replace("_", " ")] = set_id
        aliases[set_id.lower()] = set_id
    return dict(sorted(aliases.items()))


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
    if tooling_policy.get("required_scope") not in {"ALL_SUPPORTED_INSTRUMENTS", "ALL_SUPPORTED_TOOLING"}:
        return []
    if candidate.get("allowed_instruments") == []:
        return [
            "tooling_policy.required_scope=ALL_SUPPORTED_INSTRUMENTS requires "
            "allowed_instruments to contain SCISSORS, FORCEPS, CLAMPS, and RETRACTOR."
        ]
    if tooling_policy.get("required_scope") == "ALL_SUPPORTED_TOOLING" and candidate.get("selected_tool_ids") == []:
        return ["tooling_policy.required_scope=ALL_SUPPORTED_TOOLING requires selected_tool_ids to contain all tool IDs."]
    return []


def _infer_request_types(candidate: dict[str, Any]) -> list[str]:
    request_types: list[str] = []
    if candidate.get("goal") is not None:
        request_types.append("TASK_GOAL_UPDATE")
    if candidate.get("priority") is not None:
        request_types.append("PRIORITY_UPDATE")
    if candidate.get("manipulator_priority") is not None:
        request_types.append("MANIPULATOR_PRIORITY_UPDATE")
    if (
        candidate.get("allowed_instruments") is not None
        or candidate.get("excluded_instruments") is not None
        or candidate.get("selected_normalized_types") is not None
        or candidate.get("excluded_normalized_types") is not None
        or candidate.get("selected_tool_ids") is not None
        or candidate.get("excluded_tool_ids") is not None
    ):
        request_types.append("INSTRUMENT_SCOPE_UPDATE")
    if candidate.get("abnormal_strategy") is not None:
        request_types.append("ABNORMAL_STRATEGY_UPDATE")
    if candidate.get("kpi_updates"):
        request_types.append("KPI_LIMIT_UPDATE")
    if candidate.get("tooling_policy"):
        request_types.append("TOOLING_POLICY_UPDATE")
    if candidate.get("target_set_id") is not None:
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


def _dedupe_values(values: Any) -> list[Any]:
    deduped: list[Any] = []
    seen: set[Any] = set()
    for value in values or []:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped
