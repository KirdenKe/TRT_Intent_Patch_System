"""Minimal FastAPI integration surface for n8n orchestration."""

from __future__ import annotations

import os
import logging
import json
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from scenario_generation.errors import (
    OperatorResolutionRequiredError,
    ScenarioGenerationError,
    ScenarioTemplateLineBindingError,
)
from scenario_generation.generator import generate_scenario_spec
from scenario_generation.template_registry import get_template, load_template_registry
from trt_core.errors import RepositoryError
from trt_core.digital_twin_adapter import (
    HostRunnerClientError,
    build_isaac_command,
    get_isaac_health,
    get_isaac_run,
    get_isaac_result,
    isaac_host_runtime_config,
    post_isaac_dry_run,
    post_isaac_run,
    post_isaac_runs,
    read_simulation_results,
)
from trt_core.ent_demo import build_current_state, state_object_to_records
from trt_core.chat_sessions import (
    clear_chat_session,
    load_chat_session,
    merge_pending_clarification,
    save_chat_session,
)
from trt_core.intent_normalizer import (
    DOMAIN_CANDIDATE_SCHEMA,
    LLM_EXTRACTED_FIELDS_SCHEMA,
    IMPLEMENTED_MANIPULATOR_PRIORITY_POLICIES,
    build_target_set_aliases,
    build_tool_vocabulary,
    normalize_domain_candidate,
    schema_for_current_trt,
)
from trt_core.line_registry import (
    get_enabled_line_ids,
    get_line_binding,
    load_line_registry,
    resolve_line_bindings,
)
from trt_core.patch_apply import apply_intent_patch, validate_intent_patch
from trt_core.release import prepare_release, record_release_decision
from trt_core.reconciliation import load_plan
from trt_core.repository import TRTRepository
from trt_core.state_records import load_current_state, save_current_state
from trt_core.supervisor import reconcile_current_trt
from trt_core.validator import SUPPORTED_OPERATIONS
from trt_core.validator import migrate_legacy_tooling_policy


app = FastAPI(title="TRT Intent Patch Core", version="0.1.0")
repository = TRTRepository()
logger = logging.getLogger(__name__)
SIMULATION_RUNS: dict[str, dict[str, Any]] = {}
HOST_RUNNER_NOT_CONFIGURED_MESSAGE = (
    "ISAAC_HOST_RUNNER_URL is not configured. Start the Windows host runner service, "
    "then set ISAAC_HOST_RUNNER_URL=http://host.docker.internal:<port> in docker-compose.yml "
    "and recreate trt-api."
)
HOST_RUNNER_SETUP_DIAGNOSTICS = [
    "Environment changes require container recreation. Run docker compose up -d --force-recreate trt-api, not docker compose restart trt-api.",
    "Check docker compose config to verify ISAAC_HOST_RUNNER_URL is interpolated.",
    "Prefer a .env file next to docker-compose.yml.",
]


def _tail(value: str | None, limit: int = 4000) -> str:
    text = value or ""
    return text[-limit:]


def _resolve_repository_path(path: str) -> Any:
    candidate = repository.root / path
    if os.path.isabs(path):
        from pathlib import Path

        return Path(path)
    return candidate


def _host_result_missing_scenario_spec(host_result: dict[str, Any]) -> bool:
    for value in host_result.get("missing_paths") or host_result.get("errors") or []:
        if str(value).startswith("ScenarioSpec path does not exist:"):
            return True
    return False


def _scenario_template_registry_path() -> Any:
    return repository.root / "data" / "scenario_templates.json"


def _line_bindings_by_key(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        binding["line_id"]: binding
        for binding in template.get("line_bindings", [])
        if isinstance(binding, dict) and binding.get("line_id")
    }


def _state_record_keys(state_records: list[dict[str, Any]]) -> list[str]:
    return sorted(record["line_id"] for record in state_records if isinstance(record.get("line_id"), str))


def _resolve_scenario_template_id(payload: dict[str, Any]) -> str:
    if payload.get("scenario_template_id"):
        return str(payload["scenario_template_id"])
    return str(load_line_registry(repository)["default_scenario_template_id"])


def _resolve_scenario_lines(
    *,
    payload: dict[str, Any],
    released_trt: dict[str, Any],
    state_records: list[dict[str, Any]],
) -> dict[str, Any]:
    registry = load_line_registry(repository)
    registry_lines = registry["lines"]
    enabled_line_ids = sorted(line_id for line_id, line in registry_lines.items() if line.get("enabled") is True)
    affected_lines = payload.get("affected_lines") or []
    if not isinstance(affected_lines, list):
        raise HTTPException(status_code=400, detail="affected_lines must be an array.")
    simulation_scope = _normalize_simulation_scope_request(
        payload.get("simulation_scope"),
        affected_lines=affected_lines,
        enabled_line_ids=enabled_line_ids,
    )
    if simulation_scope["mode"] == "FULL_SYSTEM_DEFAULT":
        required_lines = enabled_line_ids
    elif simulation_scope["mode"] == "EXPLICIT_OPERATOR_LIMITED":
        required_lines = simulation_scope["lines"]
    else:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Unsupported simulation scope.",
                "simulation_scope": simulation_scope,
            },
        )

    trt_line_ids = sorted((released_trt.get("lines") or {}).keys())
    state_line_ids = _state_record_keys(state_records)
    missing_registry_lines = sorted(set(trt_line_ids) - set(registry_lines))
    missing_trt_lines = sorted(set(required_lines) - set(trt_line_ids))
    missing_state_lines = sorted(set(required_lines) - set(state_line_ids))
    resolved_line_bindings, missing_required_registry_lines = resolve_line_bindings(repository, required_lines)
    missing_registry_lines = sorted(set(missing_registry_lines) | set(missing_required_registry_lines))
    if missing_registry_lines or missing_trt_lines or missing_state_lines:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Scenario line resolution failed.",
                "required_lines": required_lines,
                "missing_registry_lines": missing_registry_lines,
                "missing_trt_lines": missing_trt_lines,
                "missing_state_lines": missing_state_lines,
                "simulation_scope": simulation_scope,
            },
        )
    return {
        "registry_id": registry["registry_id"],
        "simulation_scope": simulation_scope,
        "enabled_line_ids": enabled_line_ids,
        "required_lines": required_lines,
        "resolved_line_bindings": resolved_line_bindings,
        "line_bindings": [resolved_line_bindings[line_id] for line_id in required_lines],
        "missing_registry_lines": missing_registry_lines,
        "missing_state_lines": missing_state_lines,
    }


def _normalize_simulation_scope_request(
    value: Any,
    *,
    affected_lines: list[str],
    enabled_line_ids: list[str],
) -> dict[str, Any]:
    reason = "Full-system simulation is required by default because the Time-Arrival Model is a system-level variable."
    if isinstance(value, dict):
        mode = value.get("mode")
        lines = value.get("lines")
        if mode == "EXPLICIT_OPERATOR_LIMITED":
            limited_lines = sorted(dict.fromkeys(lines or affected_lines))
            return {
                "mode": "EXPLICIT_OPERATOR_LIMITED",
                "lines": limited_lines,
                "reason": value.get("reason") or "Operator explicitly requested a reduced simulation scope.",
            }
        if mode == "FULL_SYSTEM_DEFAULT":
            return {
                "mode": "FULL_SYSTEM_DEFAULT",
                "lines": list(enabled_line_ids),
                "reason": value.get("reason") or reason,
            }

    if value in {"EXPLICIT_OPERATOR_LIMITED", "PARTIAL_AFFECTED_LINES", "AFFECTED_LINES_ONLY"}:
        return {
            "mode": "EXPLICIT_OPERATOR_LIMITED",
            "lines": sorted(dict.fromkeys(affected_lines)),
            "reason": "Operator explicitly requested a reduced simulation scope.",
        }

    return {
        "mode": "FULL_SYSTEM_DEFAULT",
        "lines": list(enabled_line_ids),
        "reason": reason,
    }


def _available_trt_versions(trt_id: str) -> list[str]:
    return [record["version"] for record in repository.list_trt_version_records(trt_id)]


def _all_available_trts() -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for record in repository.list_trt_version_records():
        grouped.setdefault(record["trt_id"], []).append(record["version"])
    return [{"trt_id": trt_id, "versions": versions} for trt_id, versions in sorted(grouped.items())]


@app.get("/health")
def get_health() -> dict[str, str]:
    return {"status": "ok"}


def build_intent_context(current_trt: dict[str, Any]) -> dict[str, Any]:
    valid_line_ids = sorted((current_trt.get("lines") or {}).keys())
    tool_vocabulary = build_tool_vocabulary(current_trt)
    valid_target_set_ids = sorted((current_trt.get("tool_sets") or {}).keys())
    target_set_aliases = build_target_set_aliases(current_trt)
    tool_ids = sorted((current_trt.get("tool_catalog") or {}).keys()) or [f"tool_{index:02d}" for index in range(1, 28)]
    return {
        "current_trt": current_trt,
        "valid_line_ids": valid_line_ids,
        "valid_target_set_ids": valid_target_set_ids,
        "target_set_aliases": target_set_aliases,
        "valid_manipulator_priority_policies": IMPLEMENTED_MANIPULATOR_PRIORITY_POLICIES,
        "valid_normalized_tool_types": tool_vocabulary["normalized_types"],
        "valid_tool_ids": tool_ids,
        "tool_aliases": tool_vocabulary["aliases"],
        "tool_vocabulary": tool_vocabulary,
        "allowed_patch_operation_types": sorted(SUPPORTED_OPERATIONS),
        "editable_path_whitelist": [
            "/lines/{line_id}/goal",
            "/lines/{line_id}/allowed_instruments",
            "/lines/{line_id}/allowed_instruments/{index}",
            "/lines/{line_id}/excluded_instruments",
            "/lines/{line_id}/excluded_instruments/{index}",
            "/lines/{line_id}/selected_tool_ids",
            "/lines/{line_id}/selected_tool_ids/{index}",
            "/lines/{line_id}/excluded_tool_ids",
            "/lines/{line_id}/excluded_tool_ids/{index}",
            "/lines/{line_id}/required_tool_ids",
            "/lines/{line_id}/required_tool_ids/{index}",
            "/lines/{line_id}/target_set_id",
            "/lines/{line_id}/priority",
            "/lines/{line_id}/manipulator_priority",
            "/lines/{line_id}/manipulator_priority/policy",
            "/lines/{line_id}/manipulator_priority/ordered_tool_ids",
            "/lines/{line_id}/manipulator_priority/ordered_tool_ids/{index}",
            "/lines/{line_id}/manipulator_priority/ordered_normalized_types",
            "/lines/{line_id}/manipulator_priority/ordered_normalized_types/{index}",
            "/lines/{line_id}/manipulator_priority/tie_breaker",
            "/lines/{line_id}/manipulator_priority/enabled",
            "/lines/{line_id}/kpi/deadline_minutes",
            "/lines/{line_id}/kpi/max_downtime_seconds",
            "/lines/{line_id}/kpi/min_throughput_per_hour",
            "/lines/{line_id}/abnormal_strategy",
            "/lines/{line_id}/tooling_policy",
            "/lines/{line_id}/tooling_policy/required_scope",
        ],
        "read_only_paths": [
            "/trt_id",
            "/version",
            "/lines/{line_id}/state",
            "/lines/{line_id}/state/mode",
            "/lines/{line_id}/state/current_task",
            "/lines/{line_id}/state/wip_count",
            "/lines/{line_id}/state/last_exception",
        ],
        "enum_values": {
            "goal": ["ROUTINE_CLASSIFICATION", "TRAUMA_SET_PRIORITY", "BACKLOG_CLEARING"],
            "instrument_type": tool_vocabulary["normalized_types"],
            "tool_id": tool_ids,
            "target_set_id": valid_target_set_ids,
            "manipulator_priority_policy": IMPLEMENTED_MANIPULATOR_PRIORITY_POLICIES,
            "abnormal_strategy": ["STOP_LINE", "CONTINUE_FEASIBLE_TASKS", "ASK_OPERATOR"],
            "line_mode": ["IDLE", "RUNNING", "INTERVENTION", "PAUSED", "ERROR"],
            "tooling_required_scope": [
                "SELECTED_TOOLING",
                "NONE",
                "ALL_SUPPORTED_TOOLING",
                "ALL_SUPPORTED_INSTRUMENTS",
                "ALLOWED_INSTRUMENTS",
            ],
            "simulation_config_update_fields": [
                "add_reference_number",
                "allowed_overlap_ratio",
                "chosen_intervention_mode",
                "travel_time",
                "fix_duration",
                "resume_delay",
                "episode_success_requires_reset_cycles",
                "headless",
            ],
        },
        "llm_candidate_generation_schema": schema_for_current_trt(LLM_EXTRACTED_FIELDS_SCHEMA, current_trt),
        "domain_candidate_internal_schema": schema_for_current_trt(DOMAIN_CANDIDATE_SCHEMA, current_trt),
        "intent_patch_internal_schema": {
            "type": "object",
            "required": ["patch_id", "trt_id", "base_version", "operator_id", "intent_text", "reason", "operations", "status"],
            "properties": {
                "patch_id": {"type": "string"},
                "trt_id": {"type": "string"},
                "base_version": {"type": "string", "example": current_trt.get("version")},
                "operator_id": {"type": "string"},
                "intent_text": {"type": "string"},
                "reason": {"type": "string"},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["op", "path"],
                        "properties": {
                            "op": {"enum": sorted(SUPPORTED_OPERATIONS)},
                            "path": {"type": "string"},
                            "value": {},
                        },
                    },
                },
                "status": {"enum": ["DRAFT", "REVIEWED", "VALIDATED", "RELEASED", "REJECTED"]},
            },
        },
        "few_shot_examples": [
            {
                "name": "valid trauma set priority patch",
                "intent_patch": {
                    "patch_id": "example-trauma-priority",
                    "trt_id": current_trt.get("trt_id"),
                    "base_version": current_trt.get("version"),
                    "operator_id": "operator-demo",
                    "intent_text": "Prioritize line 1 for an incoming trauma set within 20 minutes.",
                    "reason": "Trauma set priority requires a positive deadline.",
                    "operations": [
                        {"op": "replace", "path": "/lines/line_1/goal", "value": "TRAUMA_SET_PRIORITY"},
                        {"op": "replace", "path": "/lines/line_1/priority", "value": 5},
                        {"op": "replace", "path": "/lines/line_1/kpi/deadline_minutes", "value": 20},
                    ],
                    "status": "REVIEWED",
                },
                "explanation": "Valid because the patched goal has a deadline_minutes value greater than 0.",
            },
            {
                "name": "valid exclude instrument type patch",
                "intent_patch": {
                    "patch_id": "example-exclude-instrument",
                    "trt_id": current_trt.get("trt_id"),
                    "base_version": current_trt.get("version"),
                    "operator_id": "operator-demo",
                    "intent_text": "Exclude retractors from line 1 until the station is recalibrated.",
                    "reason": "Temporary station constraint.",
                    "operations": [
                        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": ["CLAMPS", "RETRACTOR"]}
                    ],
                    "status": "REVIEWED",
                },
                "explanation": "Valid when excluded instruments do not overlap with allowed instruments.",
            },
            {
                "name": "invalid semantic conflict example",
                "intent_patch": {
                    "patch_id": "example-semantic-conflict",
                    "trt_id": current_trt.get("trt_id"),
                    "base_version": current_trt.get("version"),
                    "operator_id": "operator-demo",
                    "intent_text": "Allow clamps on line 1.",
                    "reason": "This conflicts with the current exclusion list.",
                    "operations": [
                        {"op": "replace", "path": "/lines/line_1/allowed_instruments", "value": ["SCISSORS", "CLAMPS"]}
                    ],
                    "status": "REVIEWED",
                },
                "explanation": "Invalid because CLAMPS would appear in both allowed_instruments and excluded_instruments.",
            },
        ],
    }


@app.post("/patch/validate")
def post_patch_validate(intent_patch: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_intent_patch(intent_patch, repository)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/patch/apply")
def post_patch_apply(intent_patch: dict[str, Any]) -> dict[str, Any]:
    try:
        return apply_intent_patch(intent_patch, repository)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/trt/current")
def get_trt_current(trt_id: str | None = None) -> dict[str, Any]:
    try:
        return repository.get_current_trt(trt_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/trt/versions")
def get_trt_versions(trt_id: str | None = None) -> dict[str, Any]:
    if trt_id is not None:
        return {
            "available_for_requested_trt_id": _available_trt_versions(trt_id),
            "all_available_trts": _all_available_trts(),
        }
    return {"all_available_trts": _all_available_trts()}


@app.get("/debug/trt-version-state")
def get_debug_trt_version_state(trt_id: str = "trt-demo") -> dict[str, Any]:
    return repository.trt_version_state(trt_id)


@app.get("/debug/current-manipulator-priority")
def get_debug_current_manipulator_priority(trt_id: str | None = None) -> dict[str, str]:
    try:
        current_trt = repository.get_current_trt(trt_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result: dict[str, str] = {}
    for line_id, line in sorted((current_trt.get("lines") or {}).items()):
        priority = line.get("manipulator_priority") or {}
        result[line_id] = str(priority.get("policy") or "FCFS")
    return result


@app.post("/debug/repair-current-trt")
def post_debug_repair_current_trt(trt_id: str = "trt-demo") -> dict[str, Any]:
    _require_debug_environment("current TRT repair")
    try:
        return repository.repair_current_trt(trt_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/audit/{audit_id}")
def get_audit(audit_id: str) -> dict[str, Any]:
    try:
        return repository.load_audit_bundle(audit_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/intent/context")
def get_intent_context(trt_id: str | None = None) -> dict[str, Any]:
    try:
        context = build_intent_context(repository.get_current_trt(trt_id))
        logger.info(
            "intent_context.llm_candidate_generation_schema.properties.tooling_policy=%r",
            context["llm_candidate_generation_schema"]["properties"]["tooling_policy"],
        )
        return context
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/chat/session/{session_id}")
def get_chat_session(session_id: str) -> dict[str, Any]:
    return load_chat_session(session_id, repository)


@app.put("/chat/session/{session_id}")
def put_chat_session(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return save_chat_session(session_id, payload, repository)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/chat/session/{session_id}")
def delete_chat_session(session_id: str) -> dict[str, Any]:
    return clear_chat_session(session_id, repository)


@app.post("/chat/session/{session_id}/merge-clarification")
def post_chat_session_merge_clarification(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    session = load_chat_session(session_id, repository)
    pending = session.get("pending_intent")
    if not pending:
        raise HTTPException(status_code=404, detail="No pending intent for chat session.")
    clarification_text = str(payload.get("clarification_text") or payload.get("message") or "")
    if not clarification_text.strip():
        raise HTTPException(status_code=422, detail="clarification_text is required.")
    merged = merge_pending_clarification(pending, clarification_text)
    trt_id = payload.get("trt_id") or pending.get("trt_id") or "trt-demo"
    try:
        current_trt = repository.get_current_trt(trt_id)
        candidate = {
            "patch_id": str(pending.get("patch_id") or f"patch_{uuid4()}"),
            "trt_id": current_trt["trt_id"],
            "base_version": current_trt["version"],
            "operator_id": str(merged.get("operator_id") or pending.get("operator_id") or ""),
            "intent_text": str(merged["merged_intent_text"]),
            "reason": str(merged.get("reason") or pending.get("reason") or ""),
            "excluded_instruments": None,
            "status": "DRAFT",
        }
        intent_patch = normalize_domain_candidate(candidate, current_trt)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        return {
            "session_id": session["session_id"],
            "state": session.get("state"),
            "pending_intent": pending,
            **merged,
            "resolved": False,
            "error": str(exc),
        }

    manipulator_priority = None
    target_set_id = None
    for operation in intent_patch.get("operations", []):
        path = str(operation.get("path") or "")
        if path.endswith("/manipulator_priority"):
            manipulator_priority = operation.get("value")
        elif path.endswith("/target_set_id"):
            target_set_id = operation.get("value")

    return {
        "session_id": session["session_id"],
        "state": session.get("state"),
        "pending_intent": pending,
        **merged,
        "resolved": True,
        "intent_text": merged["merged_intent_text"],
        "request_types": intent_patch.get("request_types") or [],
        "target_lines": intent_patch.get("affected_lines") or [],
        "target_set_id": target_set_id,
        "manipulator_priority": manipulator_priority,
        "simulation_config_updates": intent_patch.get("simulation_config_updates") or {},
        "candidate_patch": intent_patch,
        "intent_patch": intent_patch,
    }


@app.get("/debug/intent-schema")
def get_debug_intent_schema() -> dict[str, Any]:
    return {
        "llm_candidate_generation_fields": sorted(LLM_EXTRACTED_FIELDS_SCHEMA["properties"]),
        "llm_candidate_generation_required": LLM_EXTRACTED_FIELDS_SCHEMA.get("required", []),
        "domain_candidate_fields": sorted(DOMAIN_CANDIDATE_SCHEMA["properties"]),
        "domain_candidate_required": DOMAIN_CANDIDATE_SCHEMA.get("required", []),
    }


@app.get("/debug/intent-normalizer-runtime")
def get_debug_intent_normalizer_runtime() -> dict[str, Any]:
    return {
        "domain_candidate_fields": sorted(DOMAIN_CANDIDATE_SCHEMA["properties"].keys()),
        "llm_extracted_fields": sorted(LLM_EXTRACTED_FIELDS_SCHEMA["properties"].keys()),
        "route_model_or_schema_used_by_normalize_endpoint": (
            "FastAPI receives candidate as dict[str, Any]; "
            "trt_core.intent_normalizer.DOMAIN_CANDIDATE_SCHEMA validates /intent/normalize "
            "and /intent/normalize-domain-candidate after DomainCandidateV2 coercion."
        ),
    }


@app.post("/debug/reset-demo-trt-state")
def post_debug_reset_demo_trt_state() -> dict[str, Any]:
    if os.environ.get("APP_ENV") not in {"dev", "test"}:
        raise HTTPException(status_code=403, detail="Debug reset is only available when APP_ENV is dev or test.")

    try:
        trt = repository.get_current_trt("trt-demo")
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    line = trt.get("lines", {}).get("line_2")
    if line is None:
        raise HTTPException(status_code=404, detail="TRT line not found: line_2")

    line["state"] = {
        "mode": "RUNNING",
        "last_exception": None,
        "current_task": None,
        "wip_count": 0,
    }
    repository.save_trt(trt)
    return {
        "trt_id": trt["trt_id"],
        "version": trt["version"],
        "line_id": "line_2",
        "state": line["state"],
    }


def _require_debug_environment(action: str) -> None:
    if os.environ.get("APP_ENV") not in {"dev", "test"}:
        raise HTTPException(status_code=403, detail=f"Debug {action} is only available when APP_ENV is dev or test.")


def _reset_demo_runtime_records() -> list[dict[str, Any]]:
    existing_by_line: dict[str, dict[str, Any]] = {}
    try:
        existing_by_line = {record.get("line_id"): record for record in load_current_state(repository)}
    except RepositoryError:
        existing_by_line = {}

    records: list[dict[str, Any]] = []
    for line_id in get_enabled_line_ids(repository):
        existing = existing_by_line.get(line_id, {})
        records.append(
            {
                "line_id": line_id,
                "mode": "RUNNING",
                "last_exception": None,
                "current_task": None,
                "wip_count": 0,
                "current_instruments": [],
                "checkpoint": "NONE",
                "locked_resources": existing.get("locked_resources", []),
            }
        )
    return save_current_state(records, repository)


def _write_ent_demo_runtime_state() -> dict[str, Any]:
    state = build_current_state(repository)
    path = repository.state_dir / "current_state.json"
    path.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return state


@app.post("/debug/reset-ent-demo-state")
def post_debug_reset_ent_demo_state() -> dict[str, Any]:
    _require_debug_environment("ENT demo state reset")
    state = _write_ent_demo_runtime_state()
    return {
        "trt_id": state["active_trt_id"],
        "trt_version": state["active_trt_version"],
        "state_source": "data/state_records/current_state.json",
        "state": state,
        "state_records": state_object_to_records(state),
    }


@app.post("/debug/reset-demo-runtime-state")
def post_debug_reset_demo_runtime_state() -> dict[str, Any]:
    _require_debug_environment("runtime reset")
    try:
        return {
            "trt_id": "trt-demo",
            "state_source": "data/state_records/current_state.json",
            "state_records": _reset_demo_runtime_records(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/debug/supervisor-state")
def get_debug_supervisor_state() -> dict[str, Any]:
    try:
        return {
            "state_source": "data/state_records/current_state.json",
            "state_records": load_current_state(repository),
        }
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/debug/line-registry")
def get_debug_line_registry() -> dict[str, Any]:
    try:
        registry = load_line_registry(repository)
        return {
            "registry": registry,
            "enabled_line_ids": get_enabled_line_ids(repository),
        }
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/debug/line-binding/{line_id}")
def get_debug_line_binding(line_id: str) -> dict[str, Any]:
    try:
        return get_line_binding(repository, line_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/debug/migrate-demo-trt-tooling-policy")
def post_debug_migrate_demo_trt_tooling_policy() -> dict[str, Any]:
    if os.environ.get("APP_ENV") not in {"dev", "test"}:
        raise HTTPException(status_code=403, detail="Debug migration is only available when APP_ENV is dev or test.")

    migrated_versions: list[dict[str, Any]] = []
    for record in repository.list_trt_version_records("trt-demo"):
        trt = repository.load_trt(record["trt_id"], record["version"])
        migrated = migrate_legacy_tooling_policy(trt)
        if migrated != trt:
            repository.save_trt(migrated)
            migrated_versions.append({"trt_id": migrated["trt_id"], "version": migrated["version"]})

    return {
        "status": "MIGRATED",
        "migrated_versions": migrated_versions,
        "current_trt": repository.get_current_trt("trt-demo"),
    }


@app.post("/intent/normalize")
def post_intent_normalize(candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        logger.info("raw_llm_candidate=%r", candidate)
        logger.info("raw_llm_candidate.tooling_policy=%r", candidate.get("tooling_policy"))
        logger.info("request_body_sent_to_python.tooling_policy=%r", candidate.get("tooling_policy"))
        current_trt = repository.get_current_trt(candidate.get("trt_id"))
        intent_patch = normalize_domain_candidate(candidate, current_trt)
        logger.info("normalize_domain_candidate.intent_patch.operations=%r", intent_patch.get("operations"))
        return {"intent_patch": intent_patch}
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/intent/normalize-domain-candidate")
def post_intent_normalize_domain_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return post_intent_normalize(candidate)


@app.post("/release/prepare")
def post_release_prepare(intent_patch: dict[str, Any]) -> dict[str, Any]:
    try:
        release_record = prepare_release(intent_patch, repository)
        current_trt = repository.get_current_trt(intent_patch.get("trt_id"))
        return {
            "release_id": release_record["release_id"],
            "patch_id": release_record["patch_id"],
            "current_trt_version": current_trt["version"],
            "candidate_summary": release_record["candidate_summary"],
            "validation_results": release_record["validation_results_at_prepare"],
            "status": release_record["status"],
        }
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/release/decision")
def post_release_decision(decision_request: dict[str, Any]) -> dict[str, Any]:
    required = {"release_id", "operator_id", "decision", "comment"}
    missing = sorted(required - set(decision_request))
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing release decision fields: {', '.join(missing)}")
    if decision_request["decision"] not in {"APPROVE", "REJECT", "REQUEST_REVISION"}:
        raise HTTPException(status_code=400, detail=f"Unsupported release decision: {decision_request['decision']}")
    try:
        return record_release_decision(decision_request, repository)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/release/list")
def get_release_list() -> dict[str, Any]:
    return {"releases": repository.list_release_records()}


@app.get("/release/{release_id}")
def get_release(release_id: str) -> dict[str, Any]:
    try:
        return repository.load_release_record(release_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/state/current")
def get_state_current() -> dict[str, Any]:
    try:
        return {"state_records": load_current_state(repository)}
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/state/update")
def post_state_update(payload: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
    state_records = payload["state_records"] if isinstance(payload, dict) and "state_records" in payload else payload
    if not isinstance(state_records, list):
        raise HTTPException(status_code=400, detail="Expected a list of state records or {'state_records': [...]}")
    try:
        return {"state_records": save_current_state(state_records, repository)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/supervisor/reconcile")
def post_supervisor_reconcile(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload or {}
    missing = [field for field in ("trt_id", "trt_version") if not body.get(field)]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing supervisor reconciliation fields: {', '.join(missing)}")
    affected_lines = body.get("affected_lines") or []
    if not isinstance(affected_lines, list):
        raise HTTPException(status_code=422, detail="affected_lines must be an array.")
    try:
        state_records = body.get("state_records") or load_current_state(repository)
        return reconcile_current_trt(
            state_records,
            repository,
            body.get("trt_id"),
            body.get("trt_version"),
            body.get("release_id"),
            affected_lines,
        )
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid supervisor reconciliation input: {exc}") from exc


@app.get("/reconciliation/list")
def get_reconciliation_list() -> dict[str, Any]:
    return {"reconciliation_plans": repository.list_reconciliation_plans()}


@app.get("/reconciliation/{plan_id}")
def get_reconciliation(plan_id: str) -> dict[str, Any]:
    try:
        return load_plan(plan_id, repository)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/debug/reconciliation-plan/{plan_id}")
def get_debug_reconciliation_plan(plan_id: str) -> dict[str, Any]:
    try:
        return repository.load_reconciliation_plan(plan_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/scenario/templates")
def get_scenario_templates() -> dict[str, Any]:
    try:
        return load_template_registry(_scenario_template_registry_path())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Scenario template registry not found") from exc


@app.get("/debug/scenario-template/{template_id}")
def get_debug_scenario_template(template_id: str) -> dict[str, Any]:
    template_path = _scenario_template_registry_path()
    try:
        registry = load_template_registry(template_path)
        template = get_template(registry, template_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Scenario template registry not found") from exc
    except ScenarioGenerationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    line_bindings = _line_bindings_by_key(template)
    return {
        "template_id": template["template_id"],
        "template_path": str(template_path),
        "line_bindings": line_bindings,
        "line_binding_keys": sorted(line_bindings),
    }


@app.get("/debug/scenario-resolution")
def get_debug_scenario_resolution(
    trt_id: str,
    trt_version: str,
    scenario_template_id: str | None = None,
    affected_lines: str | None = None,
    simulation_scope: str | None = None,
) -> dict[str, Any]:
    try:
        released_trt = repository.load_trt(trt_id, trt_version)
        state_records = load_current_state(repository)
        payload = {
            "scenario_template_id": scenario_template_id,
            "affected_lines": [item for item in (affected_lines or "").split(",") if item],
            "simulation_scope": simulation_scope,
        }
        resolved_template_id = _resolve_scenario_template_id(payload)
        resolution = _resolve_scenario_lines(
            payload=payload,
            released_trt=released_trt,
            state_records=state_records,
        )
        return {
            "scenario_template_id": resolved_template_id,
            "required_lines": resolution["required_lines"],
            "resolved_line_bindings": resolution["resolved_line_bindings"],
            "missing_registry_lines": resolution["missing_registry_lines"],
            "missing_state_lines": resolution["missing_state_lines"],
            "simulation_scope": resolution["simulation_scope"],
        }
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/scenario/generate")
def post_scenario_generate(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"release_id", "trt_id", "trt_version", "reconciliation_plan_id"}
    missing = sorted(field for field in required if not payload.get(field))
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing scenario generation fields: {', '.join(missing)}")
    try:
        scenario_template_id = _resolve_scenario_template_id(payload)
        try:
            released_trt = repository.load_trt(payload["trt_id"], payload["trt_version"])
        except RepositoryError as exc:
            if "TRT version not found" in str(exc):
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": "TRT version not found",
                        "requested": {
                            "trt_id": payload["trt_id"],
                            "trt_version": payload["trt_version"],
                        },
                        "available_for_requested_trt_id": _available_trt_versions(payload["trt_id"]),
                        "all_available_trts": _all_available_trts(),
                    },
                )
            raise
        released_trt["release_id"] = payload["release_id"]
        state_records = load_current_state(repository)
        reconciliation_plan = load_plan(payload["reconciliation_plan_id"], repository)
        scenario_resolution = _resolve_scenario_lines(
            payload=payload,
            released_trt=released_trt,
            state_records=state_records,
        )
        template_path = _scenario_template_registry_path()
        template_registry = load_template_registry(template_path)
        resolved_template = get_template(template_registry, scenario_template_id)
        template_bound_lines = sorted(_line_bindings_by_key(resolved_template))
        current_trt_lines = sorted((released_trt.get("lines") or {}).keys())
        logger.info(
            "scenario_generate.template_resolution=%r",
            {
                "request.scenario_template_id": payload.get("scenario_template_id"),
                "resolved_template_id": resolved_template.get("template_id"),
                "template_file_path": str(template_path),
                "template.line_bindings.keys": template_bound_lines,
                "current_trt_line_keys": current_trt_lines,
                "required_lines": scenario_resolution["required_lines"],
                "registry.line_bindings.keys": sorted(scenario_resolution["resolved_line_bindings"]),
                "missing_line_bindings": scenario_resolution["missing_registry_lines"],
                "simulation_scope": scenario_resolution["simulation_scope"],
            },
        )
        _normalize_reconciliation_plan_version(reconciliation_plan)
        logger.info(
            "scenario_generate.version_contract=%r",
            {
                "request.trt_id": payload.get("trt_id"),
                "request.trt_version": payload.get("trt_version"),
                "request.reconciliation_plan_id": payload.get("reconciliation_plan_id"),
                "saved_plan.trt_id": reconciliation_plan.get("trt_id"),
                "saved_plan.trt_version": reconciliation_plan.get("trt_version"),
                "saved_plan.target_trt_version": reconciliation_plan.get("target_trt_version"),
                "saved_plan.released_trt_version": reconciliation_plan.get("released_trt_version"),
                "saved_plan.keys": sorted(reconciliation_plan.keys()),
            },
        )
        _validate_scenario_reconciliation_contract(payload, reconciliation_plan)
        reconciliation_plan["release_id"] = payload["release_id"]
        affected_lines = payload.get("affected_lines") or []
        request_line_decisions = payload.get("line_decisions") or []
        if not isinstance(affected_lines, list):
            raise HTTPException(status_code=400, detail="affected_lines must be an array.")
        if not isinstance(request_line_decisions, list):
            raise HTTPException(status_code=400, detail="line_decisions must be an array.")
        if any(not isinstance(decision, dict) for decision in request_line_decisions):
            raise HTTPException(status_code=400, detail="line_decisions must contain objects.")
        line_decisions = request_line_decisions or reconciliation_plan.get("line_decisions") or []
        reconciliation_plan["line_decisions"] = line_decisions
        allow_baseline = bool(payload.get("allow_baseline_on_no_change", False))
        only_no_change = bool(line_decisions) and all(
            decision.get("decision") == "NO_CHANGE" for decision in line_decisions
        )
        if only_no_change and not affected_lines and not allow_baseline:
            raise HTTPException(
                status_code=409,
                detail="Reconciliation plan contains no changed lines.",
            )
        result = generate_scenario_spec(
            released_trt=released_trt,
            state_records=state_records,
            reconciliation_plan=reconciliation_plan,
            scenario_template_id=scenario_template_id,
            candidate_strategy_id=payload.get("candidate_strategy_id") or f"strategy_{payload['reconciliation_plan_id']}",
            output_path=repository.root / "outputs",
            template_registry=template_registry,
            include_waiting_scenarios=bool(payload.get("include_waiting_scenarios", False)),
            line_bindings=scenario_resolution["line_bindings"],
            required_line_ids=scenario_resolution["required_lines"],
            simulation_scope=scenario_resolution["simulation_scope"],
            simulation_config_override=payload.get("simulation_config_updates") or payload.get("simulation_config_override"),
        )
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OperatorResolutionRequiredError as exc:
        return {
            "status": "REQUIRES_OPERATOR_RESOLUTION",
            "rejection_reason": str(exc),
            "line_id": exc.line_id,
            "field": exc.field,
            "current_value": exc.current_value,
            "allowed_values": exc.allowed_values,
        }
    except ScenarioTemplateLineBindingError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "template_id": exc.template_id,
                "required_trt_lines": exc.required_trt_lines,
                "template_bound_lines": exc.template_bound_lines,
                "missing_line_bindings": exc.missing_line_bindings,
            },
        ) from exc
    except ScenarioGenerationError as exc:
        return {"status": "REJECTED", "rejection_reason": str(exc)}
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if result.get("status") == "WAITING_FOR_CHECKPOINT":
        return result
    return {
        "status": "GENERATED",
        "scenario_spec_id": result["scenario_spec_id"],
        "scenario_spec_path": result["workspace_contract"]["expected_scenario_spec_path"],
        "scenario_spec": result,
    }


@app.post("/simulation/runs")
@app.post("/simulation/run")
def post_simulation_run(payload: dict[str, Any]) -> dict[str, Any]:
    run_mode = str(payload.get("run_mode") or "ASYNC").upper()
    if run_mode not in {"SYNC", "ASYNC"}:
        raise HTTPException(status_code=400, detail="run_mode must be SYNC or ASYNC.")
    scenario_spec_path = payload.get("scenario_spec_path")
    if not scenario_spec_path:
        return {
            "status": "FAILED",
            "run_id": None,
            "scenario_spec_id": payload.get("scenario_spec_id"),
            "output_db_path": None,
            "kpis": {},
            "stdout_tail": "",
            "stderr_tail": "",
            "errors": ["scenario_spec_path is required."],
        }

    resolved_spec_path = _resolve_repository_path(str(scenario_spec_path))
    if not resolved_spec_path.exists():
        return {
            "status": "FAILED",
            "run_id": None,
            "scenario_spec_id": payload.get("scenario_spec_id"),
            "output_db_path": None,
            "kpis": {},
            "stdout_tail": "",
            "stderr_tail": "",
            "errors": [f"ScenarioSpec file not found: {resolved_spec_path}"],
        }

    try:
        scenario_spec = json.loads(resolved_spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "status": "FAILED",
            "run_id": None,
            "scenario_spec_id": payload.get("scenario_spec_id"),
            "output_db_path": None,
            "kpis": {},
            "stdout_tail": "",
            "stderr_tail": "",
            "errors": [f"ScenarioSpec JSON is invalid: {exc}"],
        }

    command_info = build_isaac_command(
        scenario_spec,
        repository,
        scenario_spec_path=resolved_spec_path,
        headless=bool(payload.get("headless", False)),
        line_id=payload.get("line_id"),
        max_steps=payload.get("max_steps"),
        validate_script_path=False,
    )
    run_id = command_info["run_id"]
    logger.info(
        "simulation_run.start run_id=%s scenario_spec_id=%s run_mode=%s scenario_spec_path=%s",
        run_id,
        command_info["scenario_spec_id"],
        run_mode,
        resolved_spec_path,
    )
    if command_info["validation_errors"]:
        return {
            "status": "FAILED",
            "run_id": run_id,
            "scenario_spec_id": command_info["scenario_spec_id"],
            "output_db_path": command_info["output_db_path"],
            "kpis": {},
            "run_artifact": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "errors": command_info["validation_errors"],
            "execution_mode": command_info["execution_mode"],
            "host_request": command_info["host_request"],
        }

    timeout_seconds = int(
        payload.get("timeout_seconds")
        or os.environ.get("ISAAC_SIMULATION_TIMEOUT_SECONDS")
        or os.environ.get("SIMULATION_RUN_TIMEOUT_SECONDS", "5400")
    )
    host_http_timeout_seconds = int(os.environ.get("ISAAC_HOST_HTTP_TIMEOUT_SECONDS", "10"))
    execution_mode = os.environ.get("ISAAC_EXECUTION_MODE", command_info["execution_mode"])
    if execution_mode != "host_runner":
        return {
            "status": "FAILED",
            "run_id": run_id,
            "scenario_spec_id": command_info["scenario_spec_id"],
            "output_db_path": command_info["output_db_path"],
            "kpis": {},
            "run_artifact": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "errors": [f"Unsupported ISAAC_EXECUTION_MODE: {execution_mode}"],
            "execution_mode": execution_mode,
            "host_request": command_info["host_request"],
        }
    host_runner_url = os.environ.get("ISAAC_HOST_RUNNER_URL")
    if not host_runner_url:
        return {
            "status": "FAILED",
            "run_id": run_id,
            "scenario_spec_id": command_info["scenario_spec_id"],
            "output_db_path": command_info["output_db_path"],
            "kpis": {},
            "run_artifact": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "errors": [HOST_RUNNER_NOT_CONFIGURED_MESSAGE],
            "setup_diagnostics": HOST_RUNNER_SETUP_DIAGNOSTICS,
            "execution_mode": execution_mode,
            "host_request": command_info["host_request"],
        }

    host_payload = {
        **command_info["host_request"],
        "scenario_spec_path": str(payload.get("host_scenario_spec_path") or command_info["host_scenario_spec_path"]),
        "output_db_path": str(payload.get("host_output_db_path") or command_info["host_output_db_path"]),
        "timeout_seconds": timeout_seconds,
        "run_mode": run_mode,
    }
    try:
        logger.info(
            "simulation_run.host_request.start run_id=%s host_runner_url=%s timeout_seconds=%s",
            run_id,
            host_runner_url,
            host_http_timeout_seconds,
        )
        if run_mode == "ASYNC":
            host_result = post_isaac_runs(host_runner_url, host_payload, timeout_seconds=host_http_timeout_seconds)
        else:
            host_result = post_isaac_run(host_runner_url, host_payload, timeout_seconds=timeout_seconds + 5)
        logger.info(
            "simulation_run.host_request.end run_id=%s host_status=%s return_code=%s",
            run_id,
            host_result.get("status"),
            host_result.get("return_code"),
        )
    except HostRunnerClientError as exc:
        logger.exception("simulation_run.host_request.error run_id=%s", run_id)
        error_text = str(exc)
        return {
            "status": "FAILED",
            "error_code": "HOST_RUNNER_START_TIMEOUT" if "HOST_RUNNER_START_TIMEOUT" in error_text else "HOST_RUNNER_START_FAILED",
            "run_id": run_id,
            "scenario_spec_id": command_info["scenario_spec_id"],
            "output_db_path": command_info["output_db_path"],
            "kpis": {},
            "run_artifact": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "errors": [error_text],
            "execution_mode": execution_mode,
            "host_request": host_payload,
        }

    if run_mode == "ASYNC" and host_result.get("status") == "RUNNING":
        SIMULATION_RUNS[run_id] = {
            "run_id": run_id,
            "scenario_spec": scenario_spec,
            "command_info": command_info,
            "host_payload": host_payload,
            "host_runner_url": host_runner_url,
            "timeout_seconds": timeout_seconds,
            "host_http_timeout_seconds": host_http_timeout_seconds,
            "execution_mode": execution_mode,
            "result_transport": os.environ.get("ISAAC_RESULT_TRANSPORT", "shared_db"),
        }
        return {
            "status": "RUNNING",
            "run_id": run_id,
            "scenario_spec_id": command_info["scenario_spec_id"],
            "output_db_path": command_info["output_db_path"],
            "host_output_db_path": host_payload.get("output_db_path"),
            "execution_mode": execution_mode,
            "host_request": host_payload,
            "host_runner": host_result,
            "affected_lines": scenario_spec.get("affected_lines") or [],
            "simulation_scope": scenario_spec.get("simulation_scope"),
            "poll_url": f"/simulation/runs/{run_id}",
        }

    if host_result.get("status") not in {"COMPLETED", "SUCCESS"}:
        errors = list(host_result.get("errors") or [])
        if _host_result_missing_scenario_spec(host_result):
            return {
                "status": "FAILED",
                "error_code": "SCENARIO_SPEC_HOST_PATH_NOT_FOUND",
                "run_id": run_id,
                "scenario_spec_id": command_info["scenario_spec_id"],
                "output_db_path": command_info["output_db_path"],
                "kpis": {},
                "run_artifact": None,
                "stdout_tail": _tail(host_result.get("stdout_tail")),
                "stderr_tail": _tail(host_result.get("stderr_tail")),
                "errors": errors or ["ScenarioSpec host path was not found."],
                "execution_mode": execution_mode,
                "container_scenario_spec_path": command_info["container_scenario_spec_path"],
                "host_scenario_spec_path": command_info["host_scenario_spec_path"],
                "host_project_root": command_info["host_runtime_config"].get("host_project_root"),
                "host_project_root_source": command_info["host_runtime_config"].get("host_project_root_source"),
                "container_project_root": command_info["host_runtime_config"].get("container_project_root"),
                "host_request": host_payload,
                "host_runner": host_result,
                "result_transport": None,
            }
        if host_result.get("status") in {"COMPLETED_NO_RESULT_DB", "FAILED_RESULT_DB_MISSING"}:
            return {
                "status": "FAILED",
                "error_code": "SIMULATION_COMPLETED_BUT_RESULT_DB_MISSING",
                "run_id": run_id,
                "scenario_spec_id": command_info["scenario_spec_id"],
                "output_db_path": command_info["output_db_path"],
                "host_output_db_path": host_payload.get("output_db_path"),
                "seed_db_path": (host_payload.get("command_args") or {}).get("seed_db_path")
                or host_result.get("seed_db_path"),
                "kpis": {},
                "run_artifact": None,
                "stdout_tail": _tail(host_result.get("stdout_tail")),
                "stderr_tail": _tail(host_result.get("stderr_tail")),
                "return_code": host_result.get("return_code"),
                "command": host_result.get("command"),
                "errors": errors or ["Isaac completed successfully but did not produce the result DB."],
                "note": "seed_sweep.sqlite3 is an input DB, not the result DB.",
                "execution_mode": execution_mode,
                "host_request": host_payload,
                "host_runner": host_result,
                "result_transport": None,
            }
        return {
            "status": "FAILED",
            "run_id": run_id,
            "scenario_spec_id": command_info["scenario_spec_id"],
            "output_db_path": command_info["output_db_path"],
            "kpis": {},
            "run_artifact": None,
            "stdout_tail": _tail(host_result.get("stdout_tail")),
            "stderr_tail": _tail(host_result.get("stderr_tail")),
            "errors": errors + [f"Host runner status: {host_result.get('status', 'UNKNOWN')}"],
            "execution_mode": execution_mode,
            "host_request": host_payload,
            "host_runner": host_result,
            "result_transport": None,
        }

    result_transport = os.environ.get("ISAAC_RESULT_TRANSPORT", "shared_db")
    if result_transport == "host_api":
        try:
            run_artifact = get_isaac_result(host_runner_url, run_id, timeout_seconds=timeout_seconds)
        except HostRunnerClientError as exc:
            run_artifact = {
                "status": "ERROR",
                "error_code": "HOST_RESULT_API_FAILED",
                "message": str(exc),
                "summary": {"overall_success": False},
            }
    else:
        run_artifact = read_simulation_results(command_info["output_db_path"], run_id)

    errors: list[str] = list(host_result.get("errors") or [])
    scope_error = _simulation_scope_result_error(scenario_spec, run_artifact)
    if scope_error:
        run_artifact = {**run_artifact, **scope_error, "status": "ERROR"}
    if run_artifact.get("status") == "ERROR":
        errors.append(str(run_artifact.get("error_code") or run_artifact.get("message")))
    simulation_succeeded = (
        run_artifact.get("status") == "COMPLETED"
        or run_artifact.get("summary", {}).get("overall_success") is True
    )
    status = "COMPLETED" if not errors and simulation_succeeded else "FAILED"
    error_code = run_artifact.get("error_code") if run_artifact.get("status") == "ERROR" else None
    return {
        "status": status,
        "error_code": error_code,
        "run_id": run_id,
        "scenario_spec_id": command_info["scenario_spec_id"],
        "output_db_path": command_info["output_db_path"],
        "kpis": run_artifact.get("summary", {}),
        "run_artifact": run_artifact,
        "stdout_tail": _tail(host_result.get("stdout_tail")),
        "stderr_tail": _tail(host_result.get("stderr_tail")),
        "errors": errors,
        "execution_mode": execution_mode,
        "host_request": host_payload,
        "host_runner": host_result,
        "affected_lines": scenario_spec.get("affected_lines") or [],
        "simulation_scope": scenario_spec.get("simulation_scope"),
        "result_diagnostics": {
            "simulation_run_status": run_artifact.get("simulation_run_status"),
            "completed_at": run_artifact.get("completed_at"),
            "line_kpis_count": run_artifact.get("line_kpis_count"),
            "tool_events_count": run_artifact.get("tool_events_count"),
            "host_runner_return_code": host_result.get("return_code"),
            "host_runner_status": host_result.get("status"),
        } if error_code == "SIMULATION_RESULT_NOT_FINALIZED" else None,
        "result_transport": result_transport,
    }


@app.get("/simulation/runs/{run_id}")
@app.get("/simulation/run/{run_id}")
def get_simulation_run_status(run_id: str) -> dict[str, Any]:
    record = SIMULATION_RUNS.get(run_id)
    if not record:
        return {"status": "UNKNOWN", "run_id": run_id, "errors": ["Simulation run ID not found."]}
    host_runner_url = record["host_runner_url"]
    try:
        logger.info(
            "simulation_run.poll.start run_id=%s host_runner_url=%s timeout_seconds=%s",
            run_id,
            host_runner_url,
            int(record.get("host_http_timeout_seconds") or os.environ.get("ISAAC_HOST_HTTP_TIMEOUT_SECONDS", "10")),
        )
        host_result = get_isaac_run(
            host_runner_url,
            run_id,
            timeout_seconds=int(record.get("host_http_timeout_seconds") or os.environ.get("ISAAC_HOST_HTTP_TIMEOUT_SECONDS", "10")),
        )
        logger.info(
            "simulation_run.poll.end run_id=%s host_status=%s return_code=%s",
            run_id,
            host_result.get("status"),
            host_result.get("return_code"),
        )
    except HostRunnerClientError as exc:
        logger.exception("simulation_run.poll.error run_id=%s", run_id)
        return {
            "status": "FAILED",
            "error_code": "HOST_RUNNER_STATUS_TIMEOUT" if "HOST_RUNNER_STATUS_TIMEOUT" in str(exc) else "HOST_RUNNER_STATUS_FAILED",
            "run_id": run_id,
            "errors": [str(exc)],
            "host_runner_url": host_runner_url,
        }

    command_info = record["command_info"]
    scenario_spec = record["scenario_spec"]
    if host_result.get("status") == "RUNNING":
        return {
            "status": "RUNNING",
            "run_id": run_id,
            "scenario_spec_id": command_info["scenario_spec_id"],
            "output_db_path": command_info["output_db_path"],
            "host_runner": host_result,
            "affected_lines": scenario_spec.get("affected_lines") or [],
            "simulation_scope": scenario_spec.get("simulation_scope"),
        }

    if host_result.get("status") not in {"COMPLETED", "SUCCESS"}:
        errors = list(host_result.get("errors") or [])
        if host_result.get("status") in {"COMPLETED_NO_RESULT_DB", "FAILED_RESULT_DB_MISSING"}:
            error_code = "SIMULATION_COMPLETED_BUT_RESULT_DB_MISSING"
        else:
            error_code = host_result.get("status") or "HOST_RUNNER_FAILED"
        return {
            "status": "FAILED",
            "error_code": error_code,
            "run_id": run_id,
            "scenario_spec_id": command_info["scenario_spec_id"],
            "output_db_path": command_info["output_db_path"],
            "kpis": {},
            "run_artifact": None,
            "stdout_tail": _tail(host_result.get("stdout_tail")),
            "stderr_tail": _tail(host_result.get("stderr_tail")),
            "errors": errors + [f"Host runner status: {host_result.get('status', 'UNKNOWN')}"],
            "host_runner": host_result,
            "affected_lines": scenario_spec.get("affected_lines") or [],
            "simulation_scope": scenario_spec.get("simulation_scope"),
        }

    result_transport = record.get("result_transport") or os.environ.get("ISAAC_RESULT_TRANSPORT", "shared_db")
    if result_transport == "host_api":
        try:
            run_artifact = get_isaac_result(host_runner_url, run_id, timeout_seconds=record["timeout_seconds"])
        except HostRunnerClientError as exc:
            run_artifact = {
                "status": "ERROR",
                "error_code": "HOST_RESULT_API_FAILED",
                "message": str(exc),
                "summary": {"overall_success": False},
            }
    else:
        run_artifact = read_simulation_results(command_info["output_db_path"], run_id)

    errors: list[str] = list(host_result.get("errors") or [])
    scope_error = _simulation_scope_result_error(scenario_spec, run_artifact)
    if scope_error:
        run_artifact = {**run_artifact, **scope_error, "status": "ERROR"}
    if run_artifact.get("status") == "ERROR":
        errors.append(str(run_artifact.get("error_code") or run_artifact.get("message")))
    simulation_succeeded = (
        run_artifact.get("status") == "COMPLETED"
        or run_artifact.get("summary", {}).get("overall_success") is True
    )
    status = "COMPLETED" if not errors and simulation_succeeded else "FAILED"
    error_code = run_artifact.get("error_code") if run_artifact.get("status") == "ERROR" else None
    return {
        "status": status,
        "error_code": error_code,
        "run_id": run_id,
        "scenario_spec_id": command_info["scenario_spec_id"],
        "output_db_path": command_info["output_db_path"],
        "kpis": run_artifact.get("summary", {}),
        "run_artifact": run_artifact,
        "stdout_tail": _tail(host_result.get("stdout_tail")),
        "stderr_tail": _tail(host_result.get("stderr_tail")),
        "errors": errors,
        "execution_mode": record["execution_mode"],
        "host_request": record["host_payload"],
        "host_runner": host_result,
        "affected_lines": scenario_spec.get("affected_lines") or [],
        "simulation_scope": scenario_spec.get("simulation_scope"),
        "result_transport": result_transport,
    }


@app.get("/simulation/result/{run_id}")
def get_simulation_result(run_id: str) -> dict[str, Any]:
    return get_simulation_run_status(run_id)


@app.get("/debug/runtime-config")
def get_debug_runtime_config() -> dict[str, Any]:
    return {
        "isaac_execution_mode": os.environ.get("ISAAC_EXECUTION_MODE", "host_runner"),
        "isaac_host_runner_url_configured": bool(os.environ.get("ISAAC_HOST_RUNNER_URL")),
        "isaac_host_runner_url": os.environ.get("ISAAC_HOST_RUNNER_URL"),
        "isaac_host_http_timeout_seconds": int(os.environ.get("ISAAC_HOST_HTTP_TIMEOUT_SECONDS", "10")),
        "isaac_simulation_timeout_seconds": int(
            os.environ.get("ISAAC_SIMULATION_TIMEOUT_SECONDS")
            or os.environ.get("SIMULATION_RUN_TIMEOUT_SECONDS", "5400")
        ),
        "isaac_status_poll_interval_seconds": int(os.environ.get("ISAAC_STATUS_POLL_INTERVAL_SECONDS", "10")),
        "isaac_status_max_polls": int(os.environ.get("ISAAC_STATUS_MAX_POLLS", "540")),
        "simulation_run_timeout_seconds_legacy": int(os.environ.get("SIMULATION_RUN_TIMEOUT_SECONDS", "5400")),
        "isaac_result_transport": os.environ.get("ISAAC_RESULT_TRANSPORT", "shared_db"),
        "active_async_runs": sorted(SIMULATION_RUNS.keys()),
    }


def _simulation_scope_result_error(scenario_spec: dict[str, Any], run_artifact: dict[str, Any]) -> dict[str, Any] | None:
    if run_artifact.get("error_code") or run_artifact.get("status") == "ERROR":
        return None
    scope = scenario_spec.get("simulation_scope") or {}
    if not isinstance(scope, dict):
        return None
    expected_lines = scope.get("lines") or []
    if not expected_lines:
        return None
    actual_count = run_artifact.get("line_kpis_count")
    if actual_count is None:
        actual_count = len(run_artifact.get("line_kpis") or [])
    if int(actual_count or 0) >= len(expected_lines):
        return None
    return {
        "error_code": "SIMULATION_RESULT_SCOPE_MISMATCH",
        "message": "Simulation result KPI row count does not match ScenarioSpec simulation_scope lines.",
        "expected_simulation_lines": list(expected_lines),
        "expected_line_kpis_count": len(expected_lines),
        "line_kpis_count": int(actual_count or 0),
        "simulation_scope_mode": scope.get("mode"),
    }


@app.post("/debug/isaac-command-preview")
def post_debug_isaac_command_preview(payload: dict[str, Any]) -> dict[str, Any]:
    scenario_spec_path = payload.get("scenario_spec_path")
    if not scenario_spec_path:
        return {
            "status": "FAILED",
            "execution_mode": os.environ.get("ISAAC_EXECUTION_MODE", "host_runner"),
            "host_request": None,
            "expected_command_args": None,
            "errors": ["scenario_spec_path is required."],
        }
    resolved_spec_path = _resolve_repository_path(str(scenario_spec_path))
    if not resolved_spec_path.exists():
        return {
            "status": "FAILED",
            "execution_mode": os.environ.get("ISAAC_EXECUTION_MODE", "host_runner"),
            "host_request": None,
            "expected_command_args": None,
            "errors": [f"ScenarioSpec file not found: {resolved_spec_path}"],
        }
    try:
        scenario_spec = json.loads(resolved_spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "status": "FAILED",
            "execution_mode": os.environ.get("ISAAC_EXECUTION_MODE", "host_runner"),
            "host_request": None,
            "expected_command_args": None,
            "errors": [f"ScenarioSpec JSON is invalid: {exc}"],
        }

    command_info = build_isaac_command(
        scenario_spec,
        repository,
        scenario_spec_path=resolved_spec_path,
        validate_script_path=False,
    )
    return {
        "status": "READY" if not command_info["validation_errors"] else "FAILED",
        "execution_mode": command_info["execution_mode"],
        "host_runner_url": command_info["host_runner_url"],
        "run_id": command_info["run_id"],
        "scenario_spec_id": command_info["scenario_spec_id"],
        "scenario_spec_path": command_info["scenario_spec_path"],
        "container_scenario_spec_path": command_info["container_scenario_spec_path"],
        "host_scenario_spec_path": command_info["host_scenario_spec_path"],
        "output_db_path": command_info["output_db_path"],
        "container_output_db_path": command_info["container_output_db_path"],
        "host_output_db_path": command_info["host_output_db_path"],
        "host_runtime_config": command_info["host_runtime_config"],
        "host_request": command_info["host_request"],
        "command_args": command_info["command_args"],
        "arg_provenance": command_info["arg_provenance"],
        "resolved_from": command_info["resolved_from"],
        "expected_command_args": command_info["host_request"]["command_args"],
        "errors": command_info["validation_errors"],
    }


@app.get("/debug/isaac-host-runner-status")
def get_debug_isaac_host_runner_status() -> dict[str, Any]:
    execution_mode = os.environ.get("ISAAC_EXECUTION_MODE", "host_runner")
    host_runner_url = os.environ.get("ISAAC_HOST_RUNNER_URL")
    runtime_config = isaac_host_runtime_config(repository)
    sample_command = build_isaac_command(
        {
            "scenario_spec_id": "debug_sample",
            "workspace_contract": {
                "expected_scenario_spec_path": "outputs/scenario_specs/m9_contract.json",
                "run_artifacts_dir": "outputs/run_artifacts",
            },
            "simulation_config": {},
            "operator_model": {},
            "line_bindings": [],
            "line_policies": [],
            "tool_catalog": {},
        },
        repository,
        scenario_spec_path=repository.root / "outputs" / "scenario_specs" / "m9_contract.json",
        validate_script_path=False,
    )
    if not host_runner_url:
        return {
            "status": "MISSING_URL",
            "execution_mode": execution_mode,
            "host_runner_url_configured": False,
            "host_runner_url": None,
            "available": False,
            "health": None,
            "python_bat_exists": None,
            "entry_script_exists": None,
            "working_directory_exists": None,
            "host_project_root_source": runtime_config["host_project_root_source"],
            "host_project_root": runtime_config["host_project_root"],
            "container_project_root": runtime_config["container_project_root"],
            "sample_container_scenario_spec_path": sample_command["container_scenario_spec_path"],
            "sample_host_scenario_spec_path": sample_command["host_scenario_spec_path"],
            "sample_path_exists_via_host_runner": None,
            "host_runtime_config_warnings": runtime_config["warnings"],
            "errors": [HOST_RUNNER_NOT_CONFIGURED_MESSAGE],
            "setup_diagnostics": HOST_RUNNER_SETUP_DIAGNOSTICS,
        }
    try:
        health = get_isaac_health(host_runner_url, timeout_seconds=5)
    except HostRunnerClientError as exc:
        return {
            "status": "UNAVAILABLE",
            "execution_mode": execution_mode,
            "host_runner_url_configured": True,
            "host_runner_url": host_runner_url,
            "available": False,
            "health": None,
            "python_bat_exists": None,
            "entry_script_exists": None,
            "working_directory_exists": None,
            "host_project_root_source": runtime_config["host_project_root_source"],
            "host_project_root": runtime_config["host_project_root"],
            "container_project_root": runtime_config["container_project_root"],
            "sample_container_scenario_spec_path": sample_command["container_scenario_spec_path"],
            "sample_host_scenario_spec_path": sample_command["host_scenario_spec_path"],
            "sample_path_exists_via_host_runner": None,
            "host_runtime_config_warnings": runtime_config["warnings"],
            "errors": [str(exc)],
            "setup_diagnostics": [],
        }
    try:
        dry_run = post_isaac_dry_run(host_runner_url, sample_command["host_request"], timeout_seconds=5)
        sample_path_exists = not _host_result_missing_scenario_spec(dry_run)
        dry_run_errors = dry_run.get("errors") or []
    except HostRunnerClientError as exc:
        sample_path_exists = None
        dry_run_errors = [str(exc)]
    return {
        "status": "OK" if health.get("status") == "OK" else "UNAVAILABLE",
        "execution_mode": execution_mode,
        "host_runner_url_configured": True,
        "host_runner_url": host_runner_url,
        "available": health.get("status") == "OK",
        "health": health,
        "python_bat_exists": health.get("python_bat_exists"),
        "entry_script_exists": health.get("entry_script_exists"),
        "working_directory_exists": health.get("working_directory_exists"),
        "host_project_root_source": runtime_config["host_project_root_source"],
        "host_project_root": runtime_config["host_project_root"],
        "container_project_root": runtime_config["container_project_root"],
        "sample_container_scenario_spec_path": sample_command["container_scenario_spec_path"],
        "sample_host_scenario_spec_path": sample_command["host_scenario_spec_path"],
        "sample_path_exists_via_host_runner": sample_path_exists,
        "sample_dry_run_errors": dry_run_errors,
        "host_runtime_config_warnings": runtime_config["warnings"],
        "errors": [],
        "setup_diagnostics": [],
    }


def _normalize_reconciliation_plan_version(plan: dict[str, Any]) -> None:
    if not plan.get("trt_version"):
        fallback_version = plan.get("target_trt_version") or plan.get("released_trt_version")
        if fallback_version:
            plan["trt_version"] = fallback_version


def _validate_scenario_reconciliation_contract(payload: dict[str, Any], plan: dict[str, Any]) -> None:
    diagnostics = {
        "request": {
            "trt_id": payload.get("trt_id"),
            "trt_version": payload.get("trt_version"),
            "reconciliation_plan_id": payload.get("reconciliation_plan_id"),
        },
        "saved_plan": {
            "trt_id": plan.get("trt_id"),
            "trt_version": plan.get("trt_version"),
            "target_trt_version": plan.get("target_trt_version"),
            "released_trt_version": plan.get("released_trt_version"),
            "keys": sorted(plan.keys()),
        },
    }
    if not plan.get("trt_id") or not plan.get("trt_version"):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Reconciliation plan is missing trt_id or trt_version.",
                **diagnostics,
            },
        )
    if payload.get("trt_id") != plan.get("trt_id") or payload.get("trt_version") != plan.get("trt_version"):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Reconciliation plan does not match requested TRT version.",
                "request_trt_id": payload.get("trt_id"),
                "plan_trt_id": plan.get("trt_id"),
                "request_trt_version": payload.get("trt_version"),
                "plan_trt_version": plan.get("trt_version"),
                "reconciliation_plan_id": payload.get("reconciliation_plan_id"),
                **diagnostics,
            },
        )
