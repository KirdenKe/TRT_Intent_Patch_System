"""Minimal FastAPI integration surface for n8n orchestration."""

from __future__ import annotations

import os
import logging
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy
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
from trt_core.evidence_extractor import build_evidence_summary, simulated_deploy
from trt_core.chat_sessions import (
    DEFAULT_VLLM_CHAT_COMPLETIONS_URL,
    DEFAULT_VLLM_MODEL,
    clear_chat_session,
    load_chat_session,
    merge_pending_clarification,
    resolve_priority_clarification_with_vllm,
    resolve_pending_priority_clarification,
    safe_session_id,
    save_chat_session,
)
from trt_core.intent_normalizer import (
    DOMAIN_CANDIDATE_SCHEMA,
    LLM_EXTRACTED_FIELDS_SCHEMA,
    IMPLEMENTED_MANIPULATOR_PRIORITY_POLICIES,
    build_target_set_aliases,
    build_tool_vocabulary,
    explicit_simulation_config_expectations,
    normalize_domain_candidate,
    relative_time_arrival_validation_errors,
    schema_for_current_trt,
)
from trt_core.line_registry import (
    get_enabled_line_ids,
    get_line_binding,
    load_line_registry,
    resolve_line_bindings,
)
from trt_core.m12 import collect_run_metrics as collect_m12_run_metrics
from trt_core.m12 import connect_metrics_db as connect_m12_metrics_db
from trt_core.m12 import approve_pending_test as approve_m12_pending_test
from trt_core.m12 import cancel_pending_test as cancel_m12_pending_test
from trt_core.m12 import export_metrics_csv as export_m12_metrics_csv
from trt_core.m12 import log_event as log_m12_event
from trt_core.m12 import m12_dir as get_m12_dir
from trt_core.m12 import normalize_m12_approval_decision
from trt_core.m12 import prepare_next_test as prepare_m12_next_test
from trt_core.m12 import run_approved_pending_test as run_m12_approved_pending_test
from trt_core.m12 import skip_pending_test as skip_m12_pending_test
from trt_core.patch_apply import apply_intent_patch, validate_intent_patch
from trt_core.release import prepare_release, record_release_decision
from trt_core.reconciliation import load_plan
from trt_core.repository import TRTRepository
from trt_core.state_records import load_current_state, save_current_state
from trt_core.strategy_selection import (
    candidate_measurements,
    generate_candidate_batch,
    locked_line_policy_fields_from_release,
    merge_candidate_simulation_config,
    now_utc as strategy_now_utc,
    rank_candidate_runs,
)
from trt_core.supervisor import reconcile_current_trt
from trt_core.time_arrival_state import (
    load_time_arrival_state,
    save_time_arrival_state,
    time_arrival_prompt_context,
)
from trt_core.experiment_evaluation import elapsed_seconds
from trt_core.validator import SUPPORTED_OPERATIONS
from trt_core.validator import migrate_legacy_tooling_policy


app = FastAPI(title="TRT Intent Patch Core", version="0.1.0")
repository = TRTRepository()
logger = logging.getLogger(__name__)
SIMULATION_RUNS: dict[str, dict[str, Any]] = {}
STRATEGY_BATCH_WORKERS: dict[str, threading.Thread] = {}
STRATEGY_SIMULATION_LOCK = threading.Lock()
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


def _record_m12_event(event_name: str, *, source_module: str, payload: dict[str, Any] | None = None, **fields: Any) -> None:
    try:
        log_m12_event(
            event_name=event_name,
            repository=repository,
            source_module=source_module,
            payload=payload,
            **fields,
        )
    except Exception:
        logger.exception("m12_event_log_failed event_name=%s", event_name)


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
    trt_line_ids = set((released_trt.get("lines") or {}).keys())
    enabled_line_ids = sorted(
        line_id
        for line_id, line in registry_lines.items()
        if line.get("enabled") is True and line_id in trt_line_ids
    )
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


@app.get("/reports/m12/runs/{run_id}/metrics")
def get_m12_run_metrics(run_id: str) -> dict[str, Any]:
    try:
        collect_m12_run_metrics(repository, run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    with connect_m12_metrics_db(repository=repository) as connection:
        row = connection.execute("SELECT * FROM m12_run_metrics WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"M12 metrics not found for run_id={run_id}")
    return dict(row)


@app.get("/reports/m12/export.csv")
def get_m12_metrics_csv() -> dict[str, Any]:
    export_m12_metrics_csv(repository=repository)
    path = get_m12_dir(repository) / "m12_metrics.csv"
    return {"path": str(path), "exists": path.exists()}


@app.get("/reports/m12/error-interception.csv")
def get_m12_error_interception_csv() -> dict[str, Any]:
    export_m12_metrics_csv(repository=repository)
    path = get_m12_dir(repository) / "m12_error_interception.csv"
    return {"path": str(path), "exists": path.exists()}


@app.get("/reports/m12/figures")
def get_m12_figures() -> dict[str, Any]:
    figure_dir = get_m12_dir(repository) / "figures"
    figures = sorted(str(path) for path in figure_dir.glob("*") if path.suffix.lower() in {".png", ".svg"})
    return {"path": str(figure_dir), "figures": figures}


def _m12_pending_path() -> Any:
    return get_m12_dir(repository) / "pending_test.json"


@app.post("/m12/tests/prepare-next")
def post_m12_prepare_next(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    pending = prepare_m12_next_test(
        suite=str(payload.get("suite") or "comparison"),
        output=_m12_pending_path(),
        seed_data=payload.get("seed_data") or get_m12_dir(repository) / "seed_data",
        repository=repository,
        chat_session_id=payload.get("chat_session_id") or payload.get("session_id"),
    )
    if payload.get("chat_session_id") or payload.get("session_id"):
        save_chat_session(
            str(payload.get("chat_session_id") or payload.get("session_id")),
            {"state": "WAITING_FOR_M12_TEST_APPROVAL", "pending_m12_test": pending},
            repository,
        )
    return {"status": "WAITING_FOR_M12_TEST_APPROVAL", "pending_test": pending}


@app.post("/m12/tests/approve")
def post_m12_approve(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    pending = approve_m12_pending_test(_m12_pending_path(), operator_id=payload.get("operator_id"))
    return {"status": "APPROVED", "pending_test": pending}


@app.post("/m12/tests/skip")
def post_m12_skip(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    pending = skip_m12_pending_test(_m12_pending_path(), operator_id=payload.get("operator_id"))
    return {"status": "SKIPPED", "pending_test": pending}


@app.post("/m12/tests/cancel")
def post_m12_cancel(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    pending = cancel_m12_pending_test(_m12_pending_path())
    session_id = payload.get("chat_session_id") or payload.get("session_id")
    if session_id:
        clear_chat_session(str(session_id), repository)
    return {"status": "CANCELLED", "pending_test": pending}


@app.post("/m12/tests/run-approved")
def post_m12_run_approved(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        result = run_m12_approved_pending_test(_m12_pending_path(), repository=repository)
    except RuntimeError as exc:
        if str(exc) == "M12_TEST_NOT_APPROVED":
            raise HTTPException(status_code=409, detail="M12_TEST_NOT_APPROVED") from exc
        raise
    return {"status": result.get("status"), "result": result}


@app.get("/m12/tests/status/{test_case_id}")
def get_m12_test_status(test_case_id: str) -> dict[str, Any]:
    pending_path = _m12_pending_path()
    pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else None
    semi_manual_dir = get_m12_dir(repository) / "semi_manual_runs"
    results = []
    if semi_manual_dir.exists():
        for path in sorted(semi_manual_dir.glob(f"{test_case_id}*.json")):
            results.append(json.loads(path.read_text(encoding="utf-8")))
    return {"test_case_id": test_case_id, "pending_test": pending, "results": results}


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
        context["time_arrival_state"] = time_arrival_prompt_context(repository)
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


def _operator_facing_text(value: str | None) -> str:
    text = str(value or "")
    replacements = [
        ("add_reference_number", "simulated tooling count"),
        ("add reference number", "simulated tooling count"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
        text = text.replace(old.upper(), new)
    return text


def _post_json(url: str, body: dict[str, Any], timeout_seconds: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if len(detail) > 2000:
            detail = f"{detail[:2000]}..."
        raise OSError(
            f"Model server returned HTTP {exc.code}"
            + (f": {detail}" if detail else ".")
        ) from exc


def _parse_json_object_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ValueError("Model output must be a JSON object or JSON string.")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Model JSON output must be an object.")
    return value


def _session_conversation(session: dict[str, Any], latest_user_message: str) -> list[dict[str, str]]:
    conversation = [
        {"role": str(turn.get("role") or ""), "content": str(turn.get("content") or "")}
        for turn in session.get("conversation", [])
        if isinstance(turn, dict) and turn.get("role") and turn.get("content")
    ]
    pending = session.get("pending_intent") or {}
    if not conversation and pending:
        original = pending.get("original_intent_text") or pending.get("intent_text")
        if original:
            conversation.append({"role": "user", "content": str(original)})
        if pending.get("operator_id") or pending.get("reason"):
            fields = []
            if pending.get("operator_id"):
                fields.append(f"operator_id: {pending['operator_id']}")
            if pending.get("reason"):
                fields.append(f"reason: {pending['reason']}")
            conversation.append({"role": "user", "content": " ".join(fields)})
        if pending.get("pending_question"):
            conversation.append({"role": "assistant", "content": _operator_facing_text(str(pending["pending_question"]))})
    if latest_user_message and (not conversation or conversation[-1] != {"role": "user", "content": latest_user_message}):
        conversation.append({"role": "user", "content": latest_user_message})
    return conversation


def _dialogue_decision_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "dialogue_state",
            "turn_type",
            "operator_message",
            "normalized_request",
            "missing_or_unclear_items",
            "approval_decision",
            "deployment_decision",
        ],
        "properties": {
            "dialogue_state": {
                "type": "string",
                "enum": [
                    "READY_FOR_REVIEW",
                    "NEEDS_CLARIFICATION",
                    "APPROVAL_DECISION",
                    "DEPLOYMENT_DECISION",
                    "HELP",
                    "CONFIG_QUERY",
                    "CANCELLED",
                    "UNKNOWN",
                ],
            },
            "turn_type": {
                "type": "string",
                "enum": [
                    "SMALL_TALK",
                    "TASK_REQUEST",
                    "CLARIFICATION_VALUES",
                    "APPROVAL_DECISION",
                    "DEPLOYMENT_DECISION",
                    "HELP",
                    "CONFIG_QUERY",
                    "CANCEL",
                    "CONFUSED",
                    "UNKNOWN",
                ],
            },
            "operator_message": {"type": "string"},
            "normalized_request": {
                "type": ["object", "null"],
                "required": [
                    "operator_id",
                    "reason",
                    "intent_text",
                    "target_scope",
                    "target_lines",
                    "target_set_id",
                    "request_types",
                    "kpi_updates",
                    "manipulator_priority",
                    "simulation_config_updates",
                ],
                "properties": {
                    "operator_id": {"type": ["string", "null"]},
                    "reason": {"type": ["string", "null"]},
                    "intent_text": {"type": ["string", "null"]},
                    "target_scope": {"type": ["string", "null"], "enum": ["ALL_LINES", "MULTIPLE_LINES", "SINGLE_LINE", None]},
                    "target_lines": {"type": "array", "items": {"type": "string"}},
                    "target_set_id": {"type": ["string", "null"], "enum": ["ENT_SURGICAL_TOOLING_SET", None]},
                    "request_types": {"type": "array", "items": {"type": "string"}},
                    "kpi_updates": {
                        "type": ["object", "null"],
                        "properties": {
                            "min_throughput_per_hour": {"type": ["integer", "null"]},
                            "deadline_minutes": {"type": ["number", "null"]},
                            "max_downtime_seconds": {"type": ["number", "null"]},
                        },
                        "additionalProperties": False,
                    },
                    "manipulator_priority": {
                        "type": ["object", "null"],
                        "properties": {
                            "enabled": {"type": "boolean"},
                            "policy": {"type": "string", "enum": ["REQUIRED_FIRST", "FCFS", "UNWANTED_FIRST", "EXPLICIT_TOOL_ORDER", "EXPLICIT_TYPE_ORDER"]},
                            "scope": {"type": ["string", "null"], "enum": ["TABLE_BATCH", None]},
                        },
                        "additionalProperties": False,
                    },
                    "simulation_config_updates": {
                        "type": ["object", "null"],
                        "properties": {
                            "dry_run_only": {"type": ["boolean", "null"]},
                            "num_envs": {"type": ["integer", "null"], "minimum": 1},
                            "chosen_intervention_mode": {
                                "type": ["string", "null"],
                                "enum": ["continue-until-arrival", "immediate-stop", None],
                            },
                            "travel_time": {"type": ["number", "null"], "minimum": 0},
                            "fix_duration": {"type": ["number", "null"], "minimum": 0},
                            "resume_delay": {"type": ["number", "null"], "minimum": 0},
                            "add_reference_number": {"type": ["integer", "null"], "minimum": 0},
                        },
                        "additionalProperties": False,
                    },
                    "dry_run_only": {"type": ["boolean", "null"]},
                    "deployment_allowed_after_success": {"type": ["boolean", "null"]},
                    "failure_action_hint": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
            "action": {
                "type": ["string", "null"],
                "enum": ["PROPOSE_PATCH", "PROPOSE_DRY_RUN", "NEEDS_CLARIFICATION", "UNKNOWN", None],
            },
            "query_targets": {
                "type": ["array", "null"],
                "items": {
                    "type": "string",
                    "enum": [
                        "TIME_ARRIVAL_MODEL",
                        "STATE_RECORDS",
                        "LINE_STATE",
                        "KPI_TARGETS",
                        "TASK_REQUIREMENT_TABLE",
                        "TRT_CURRENT",
                        "TRT_HISTORY",
                        "DEPLOYMENT_HISTORY",
                        "SCENARIO_SPEC",
                        "RUN_ARTIFACT",
                        "ISAAC_COMMAND_CONFIG",
                    ],
                },
            },
            "line_ids": {"type": ["array", "null"], "items": {"type": "string"}},
            "scenario_spec_id": {"type": ["string", "null"]},
            "run_id": {"type": ["string", "null"]},
            "missing_or_unclear_items": {"type": "array", "items": {"type": "string"}},
            "approval_decision": {"type": ["string", "null"], "enum": ["APPROVE", "REJECT", "REQUEST_REVISION", None]},
            "deployment_decision": {
                "type": ["string", "null"],
                "enum": ["DEPLOY", "DEPLOY_WITH_ACK", "DO_NOT_DEPLOY", "RERUN_SIMULATION", "REQUEST_REVISION", None],
            },
        },
    }


def _build_dialogue_decision_prompt(payload: dict[str, Any], session: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    latest = str(payload.get("latest_user_message") or payload.get("raw_chat_input") or "")
    conversation = _session_conversation(session, latest)
    current_trt = repository.get_current_trt()
    current_time_arrival = time_arrival_prompt_context(repository)
    pending_intent = session.get("pending_intent") or {}
    normalized_session_request = session.get("normalized_request") or {}
    active_request = {
        "session_state": session.get("state") or "IDLE",
        "original_user_request": next((turn["content"] for turn in conversation if turn["role"] == "user"), ""),
        "operator_id": normalized_session_request.get("operator_id") or pending_intent.get("operator_id"),
        "reason": normalized_session_request.get("reason") or pending_intent.get("reason"),
        "prior_clarification_questions": [turn["content"] for turn in conversation if turn["role"] == "assistant"],
        "prior_clarification_answers": [
            turn["content"]
            for index, turn in enumerate(conversation)
            if turn["role"] == "user" and index > 0
        ],
        "pending_intent": pending_intent or None,
        "candidate_patch_summary": session.get("candidate_patch_summary"),
        "review_status": session.get("review_status"),
        "approval_status": session.get("approval_status"),
        "scenario_spec_id": session.get("scenario_spec_id"),
        "run_id": session.get("run_id"),
        "pending_evidence": session.get("pending_evidence"),
        "pending_deployment": session.get("pending_deployment"),
        "allowed_actions": session.get("allowed_actions") or [],
    }
    domain_context = {
        "valid_lines": sorted((current_trt.get("lines") or {}).keys()) or ["line_1", "line_2", "line_3", "line_4"],
        "known_tool_sets": sorted((current_trt.get("tool_sets") or {}).keys()) or ["ENT_SURGICAL_TOOLING_SET"],
        "supported_request_types": [
            "TOOLING_POLICY_UPDATE",
            "MANIPULATOR_PRIORITY_UPDATE",
            "SIMULATION_CONFIG_UPDATE",
            "KPI_UPDATE",
        ],
        "time_arrival_state": current_time_arrival,
    }
    decision_input = {
        "latest_user_message": latest,
        "conversation": conversation,
        "active_request": active_request,
        "domain_context": domain_context,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are the only component that classifies the operator's chat turn. "
                "Return turn_type as one of SMALL_TALK, TASK_REQUEST, CLARIFICATION_VALUES, APPROVAL_DECISION, "
                "DEPLOYMENT_DECISION, HELP, CONFIG_QUERY, CANCEL, CONFUSED, or UNKNOWN. "
                "Read the full conversation and decide whether the request is ready for review, needs one clarification, "
                "is an approval/deployment decision, is cancelled, or is unknown. "
                "Do not require operators to use internal schema terms such as request_type. "
                "Classify requests for help, usage guidance, or examples as HELP. "
                "Classify casual greetings and filler as SMALL_TALK with normalized_request null. "
                "Classify production-line KPI changes as TASK_REQUEST. "
                "Classification and field extraction are separate judgments. If the operator clearly asks to change "
                "production-line, simulation, Time-Arrival, KPI, tooling, or priority behavior, always classify the "
                "turn as TASK_REQUEST even when one extracted field is uncertain or arithmetically invalid. Never "
                "return UNKNOWN solely because simulation_config_updates is incomplete or fails semantic validation. "
                "Classify questions about current configuration, current Time-Arrival Model parameters, current KPI targets, "
                "state records, one production line's state, past requirement tables, current TRT, previous deployments, "
                "ScenarioSpecs, run artifacts, or Isaac command configuration as CONFIG_QUERY. "
                "For CONFIG_QUERY, set query_targets to the requested source category, extract line_ids/scenario_spec_id/run_id "
                "when the operator names them, and do not create a patch. "
                "If a pending task exists and the user provides operator_id or reason, classify the turn as CLARIFICATION_VALUES. "
                "If session_state is WAITING_FOR_POST_EVIDENCE_DECISION, classify REQUEST_REVISION or revise as DEPLOYMENT_DECISION "
                "with deployment_decision REQUEST_REVISION, classify RERUN_SIMULATION or rerun it as DEPLOYMENT_DECISION with "
                "deployment_decision RERUN_SIMULATION, and classify cancel as CANCEL. Do not treat those replies as new task requests. "
                "For throughput/hr, throughput per hour, min throughput, or minimum throughput requests, set "
                "normalized_request.kpi_updates.min_throughput_per_hour to the requested number and include KPI_UPDATE in request_types. "
                "Do not ask the same clarification twice if the user answered it semantically. "
                "Map 'number of tooling so only N remain' to simulation_config_updates.add_reference_number=N, "
                "but never mention add_reference_number to the operator; say simulated tooling count. "
                "For Time-Arrival Model dry-run requests, extract simulation_config_updates directly. "
                "For relative Time-Arrival changes, use domain_context.time_arrival_state as the only baseline. "
                "Interpret the operator's relative timing language and independently calculate the resulting absolute values. "
                "A later IntentPatch validation stage will independently validate these values; this routing decision "
                "must preserve the turn classification regardless of that later result. "
                "Do not use memorized or example default values. "
                "Map only two production lines remaining to simulation_config_updates.num_envs=2 and target_scope MULTIPLE_LINES. "
                "Map stop robotic arms immediately on anomaly to simulation_config_updates.chosen_intervention_mode='immediate-stop' "
                "and include ABNORMAL_STRATEGY_UPDATE. If ABNORMAL_STRATEGY_UPDATE is included because the operator requested "
                "immediate stopping on anomaly, the response is incomplete unless chosen_intervention_mode is present. "
                "Map number of tooling per production line to 6 to add_reference_number=6. "
                "Do not set dry_run_only just because the operator says confirm, verify, validate, or wants to know whether a "
                "configuration can work. Those are normal deployable TASK_REQUEST turns unless the operator explicitly says "
                "dry run only, dry run, test only, simulate only, no deployment, or do not deploy. "
                "Only when the operator explicitly requests dry-run/no-deployment behavior, set action PROPOSE_DRY_RUN, "
                "dry_run_only true, deployment_allowed_after_success false, and include DRY_RUN_ONLY in request_types. "
                "If the user says robots pick ENT surgical tooling/tools/set first, that means MANIPULATOR_PRIORITY_UPDATE "
                "with REQUIRED_FIRST and scope TABLE_BATCH. Preserve target lines from the conversation unless the user explicitly says all lines. "
                "READY_FOR_REVIEW requires operator_id, reason, target lines or ALL_LINES, request_types, and a complete normalized_request. "
                "If operator_id or reason is missing, return NEEDS_CLARIFICATION and ask only for the missing fields. "
                "If the previous assistant asked whether this is production-line priority or robot ENT-required-first picking, and the latest user says "
                "robots pick ENT surgical tooling set first, return READY_FOR_REVIEW, not another clarification. "
                "Examples: input 'yo dude' with session_state IDLE returns turn_type SMALL_TALK, dialogue_state UNKNOWN, "
                "operator_id null, reason null, intent_text null, and decision null. "
                "Input 'help' returns turn_type HELP, dialogue_state HELP, query_targets [], and normalized_request null. "
                "Input 'i want to set all line\\'s throughput/hr back to 60' with session_state IDLE returns turn_type TASK_REQUEST, "
                "dialogue_state NEEDS_CLARIFICATION, intent_text 'set all line\\'s throughput/hr back to 60', "
                "target_scope ALL_LINES, target_lines [], request_types ['KPI_UPDATE'], "
                "kpi_updates {'min_throughput_per_hour': 60}, operator_id null, and reason null. "
                "Input 'operator_id: op_001 reason: test for milestone 11.5' with session_state WAITING_FOR_REQUIRED_FIELDS "
                "and pending_intent.intent_text 'set all line\\'s throughput/hr back to 60' returns turn_type CLARIFICATION_VALUES, "
                "dialogue_state NEEDS_CLARIFICATION or READY_FOR_REVIEW depending on whether the normalized_request is complete, "
                "operator_id 'op_001', reason 'test for milestone 11.5', and intent_text null unless restating the task. "
                "Input 'What are the current Time-Arrival Model parameters?' returns turn_type CONFIG_QUERY, "
                "dialogue_state CONFIG_QUERY, query_targets ['TIME_ARRIVAL_MODEL'], and normalized_request null. "
                "Input 'show me production line 1 state record' returns turn_type CONFIG_QUERY, "
                "dialogue_state CONFIG_QUERY, query_targets ['LINE_STATE'], line_ids ['line_1'], and normalized_request null. "
                "Input 'show me the task requirements table' returns turn_type CONFIG_QUERY, "
                "dialogue_state CONFIG_QUERY, query_targets ['TASK_REQUIREMENT_TABLE'], and normalized_request null. "
                "A composite Time-Arrival input returns TASK_REQUEST and READY_FOR_REVIEW when operator_id and reason "
                "are present, action PROPOSE_PATCH, dry_run_only false, and includes both SIMULATION_CONFIG_UPDATE "
                "and ABNORMAL_STRATEGY_UPDATE. Its numeric values must be derived from domain_context.time_arrival_state. "
                "Input beginning 'dry run only' with the same Time-Arrival settings returns action PROPOSE_DRY_RUN, "
                "dry_run_only true, and includes DRY_RUN_ONLY in request_types. "
                "Return only JSON matching the schema."
            ),
        },
        {"role": "user", "content": json.dumps(decision_input, sort_keys=True)},
    ]
    return messages, decision_input


def _dialogue_time_arrival_validation_errors(
    decision: dict[str, Any],
    decision_input: dict[str, Any],
) -> list[str]:
    normalized = decision.get("normalized_request")
    if not isinstance(normalized, dict):
        normalized = {}
    candidate = {
        **normalized,
        "intent_text": (
            normalized.get("intent_text")
            or (decision_input.get("active_request") or {}).get("original_user_request")
            or decision_input.get("latest_user_message")
            or ""
        ),
    }
    baseline = (decision_input.get("domain_context") or {}).get("time_arrival_state") or {}
    return relative_time_arrival_validation_errors(candidate, baseline)


def _resolved_dialogue_intent_text(normalized_request: dict[str, Any], fallback: str) -> str:
    if normalized_request.get("kpi_updates"):
        return str(normalized_request.get("intent_text") or fallback or "")
    priority = normalized_request.get("manipulator_priority") or {}
    lines = list(normalized_request.get("target_lines") or [])
    if normalized_request.get("target_scope") == "ALL_LINES":
        target_phrase = "all production lines"
    elif lines:
        target_phrase = ", ".join(lines)
    else:
        target_phrase = "the selected production lines"
    parts: list[str] = []
    if normalized_request.get("target_set_id"):
        parts.append(f"Set {target_phrase} to target the ENT surgical tooling set.")
    if priority.get("policy") == "REQUIRED_FIRST":
        parts.append(f"Make the robots on {target_phrase} pick ENT-required tooling first.")
    updates = normalized_request.get("simulation_config_updates") or {}
    if updates.get("add_reference_number") is not None:
        parts.append(f"Set the simulated tooling count to {updates['add_reference_number']}.")
    return " ".join(parts) or str(normalized_request.get("intent_text") or fallback or "")


def _dialogue_state_for_turn(turn_type: str, default: str = "UNKNOWN") -> str:
    return {
        "APPROVAL_DECISION": "APPROVAL_DECISION",
        "DEPLOYMENT_DECISION": "DEPLOYMENT_DECISION",
        "HELP": "HELP",
        "CONFIG_QUERY": "CONFIG_QUERY",
        "CANCEL": "CANCELLED",
        "SMALL_TALK": "UNKNOWN",
        "CONFUSED": "UNKNOWN",
        "UNKNOWN": "UNKNOWN",
    }.get(turn_type, default)


def _turn_type_for_dialogue_state(dialogue_state: str) -> str:
    return {
        "APPROVAL_DECISION": "APPROVAL_DECISION",
        "DEPLOYMENT_DECISION": "DEPLOYMENT_DECISION",
        "HELP": "HELP",
        "CONFIG_QUERY": "CONFIG_QUERY",
        "CANCELLED": "CANCEL",
        "UNKNOWN": "UNKNOWN",
    }.get(dialogue_state, "TASK_REQUEST")


def _internal_request_types(request_types: Any) -> list[str]:
    internal: list[str] = []
    for value in request_types or []:
        text = str(value or "")
        if text == "KPI_UPDATE":
            text = "KPI_LIMIT_UPDATE"
        if text and text not in internal:
            internal.append(text)
    return internal


def _load_default_simulation_config_for_chat() -> dict[str, Any]:
    path = repository.root / "data" / "digital_twin" / "default_simulation_config.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    config = payload.get("simulation_config")
    return dict(config) if isinstance(config, dict) else {}


def _load_template_simulation_config_for_chat() -> dict[str, Any]:
    try:
        registry = load_template_registry(repository.root / "data" / "scenario_templates.json")
        template = get_template(registry, None)
        config = template.get("simulation_config") or {}
        operator_model = template.get("operator_model") or {}
        merged = dict(config)
        for key in ("travel_time", "fix_duration", "resume_delay"):
            if key not in merged and operator_model.get(key) is not None:
                merged[key] = operator_model[key]
        return merged
    except Exception:
        return {}


def _chat_help_message() -> str:
    return (
        "I can help with four kinds of requests:\n\n"
        "1. Change production-line requirements\n"
        "   Example: set all production lines throughput/hr to at least 60.\n\n"
        "2. Change tooling targets or picking order\n"
        "   Example: set lines 2 and 4 to target retractors.\n"
        "   Example: make lines 1 and 3 pick non-forceps tools first.\n\n"
        "3. Ask about current configuration\n"
        "   Example: what are the current Time-Arrival Model parameters?\n"
        "   Example: show the state record for line 1.\n"
        "   Example: how are the KPIs currently set?\n\n"
        "4. Review simulation and deployment results\n"
        "   Example: show the latest run artifact.\n"
        "   Example: why did the KPI check fail?\n\n"
        "For a change request, include operator_id and reason when you are ready."
    )


def _load_current_state_object() -> dict[str, Any]:
    path = repository.root / "data" / "state_records" / "current_state.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_json_file(directory: str) -> Any:
    path = repository.root / directory
    files = sorted(path.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True) if path.exists() else []
    return files[0] if files else None


def _latest_sqlite_file(directory: str) -> Any:
    path = repository.root / directory
    files = sorted(path.glob("*.sqlite"), key=lambda item: item.stat().st_mtime, reverse=True) if path.exists() else []
    return files[0] if files else None


def _line_state_answer(state: dict[str, Any], line_ids: list[str]) -> dict[str, Any]:
    lines = state.get("lines") if isinstance(state.get("lines"), dict) else {}
    requested = line_ids or sorted(lines.keys())
    rows = []
    missing = []
    for line_id in requested:
        row = lines.get(line_id)
        if not isinstance(row, dict):
            missing.append(line_id)
            continue
        rows.append(
            {
                "line_id": line_id,
                "mode": row.get("mode"),
                "active_set_id": row.get("active_set_id"),
                "current_task": row.get("current_task"),
                "wip_count": row.get("wip_count"),
                "selected_tool_ids": row.get("selected_tool_ids") or [],
                "pending_tool_ids": row.get("pending_tool_ids") or [],
                "completed_tool_ids": row.get("completed_tool_ids") or [],
                "entanglement": row.get("entanglement") or {},
                "locked_resources": row.get("locked_resources") or [],
                "last_exception": row.get("last_exception"),
                "robot_id": row.get("robot_id"),
                "workspace_id": row.get("workspace_id"),
            }
        )
    return {"lines": rows, "missing_line_ids": missing}


def _task_requirement_table_from_spec(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for policy in spec.get("line_policies") or []:
        if not isinstance(policy, dict):
            continue
        priority = policy.get("manipulator_priority") or {}
        kpi = policy.get("kpi") or {}
        rows.append(
            {
                "line_id": policy.get("line_id"),
                "target_set_id": policy.get("target_set_id"),
                "selected_tool_ids": policy.get("selected_tool_ids") or [],
                "excluded_tool_ids": policy.get("excluded_tool_ids") or [],
                "required_tool_ids": policy.get("required_tool_ids") or [],
                "selected_normalized_types": policy.get("selected_normalized_types") or [],
                "excluded_normalized_types": policy.get("excluded_normalized_types") or [],
                "manipulator_priority": {
                    "policy": priority.get("policy"),
                    "enabled": priority.get("enabled"),
                    "ordered_tool_ids": priority.get("ordered_tool_ids") or [],
                    "ordered_normalized_types": priority.get("ordered_normalized_types") or [],
                },
                "kpi_target": {
                    "min_throughput_per_hour": kpi.get("min_throughput_per_hour"),
                    "deadline_minutes": kpi.get("deadline_minutes"),
                    "max_downtime_seconds": kpi.get("max_downtime_seconds"),
                },
            }
        )
    return rows


def _latest_run_artifact_summary(run_id: str | None = None) -> tuple[dict[str, Any], str | None]:
    db_path = repository.root / "outputs" / "run_artifacts" / f"{run_id}.sqlite" if run_id else _latest_sqlite_file("outputs/run_artifacts")
    if not db_path or not db_path.exists():
        return {"error": "No run artifact SQLite file was found."}, None
    resolved_run_id = run_id or db_path.stem
    try:
        artifact = read_simulation_results(db_path, resolved_run_id)
    except Exception as exc:
        return {"run_id": resolved_run_id, "error": str(exc)}, str(db_path.relative_to(repository.root))
    return (
        {
            "run_id": resolved_run_id,
            "simulation_runs": artifact.get("simulation_runs") or [],
            "line_kpis": artifact.get("line_kpis") or [],
            "container_completion_events": artifact.get("container_completion_events") or [],
            "warnings": artifact.get("warnings") or [],
        },
        str(db_path.relative_to(repository.root)),
    )


def _build_config_query_answer(
    *,
    query_targets: list[str],
    line_ids: list[str] | None = None,
    scenario_spec_id: str | None = None,
    run_id: str | None = None,
    raw_chat_input: str = "",
) -> dict[str, Any]:
    targets = set(query_targets or [])
    if not targets:
        targets = {"TIME_ARRIVAL_MODEL"}
    requested_line_ids = set(line_ids or [])
    defaults = _load_template_simulation_config_for_chat()
    deployed = _load_default_simulation_config_for_chat()
    config = {**defaults, **{key: value for key, value in deployed.items() if value is not None}}
    sources: list[str] = []
    structured: dict[str, Any] = {"query_targets": sorted(targets), "raw_chat_input": raw_chat_input}
    state = _load_current_state_object()
    if targets & {"STATE_RECORDS", "LINE_STATE"}:
        sources.append("data/state_records/current_state.json")
        lines = state.get("lines") if isinstance(state.get("lines"), dict) else {}
        visible_lines = {line_id: line for line_id, line in lines.items() if not requested_line_ids or line_id in requested_line_ids}
        running = sorted(line_id for line_id, line in visible_lines.items() if isinstance(line, dict) and line.get("mode") == "RUNNING")
        structured["state_record"] = {
            "active_trt_id": state.get("active_trt_id"),
            "active_trt_version": state.get("active_trt_version"),
            "state_version": state.get("state_version"),
            "deployment_id": state.get("last_deployment_id"),
            "running_lines": running,
            "line_modes": {line_id: line.get("mode") for line_id, line in visible_lines.items() if isinstance(line, dict)},
        }
    if "LINE_STATE" in targets:
        structured["line_state"] = _line_state_answer(state, line_ids or [])
    if "TIME_ARRIVAL_MODEL" in targets or "ISAAC_COMMAND_CONFIG" in targets:
        sources.extend(["data/digital_twin/default_simulation_config.json", "data/scenario_templates.json"])
        structured["time_arrival_model"] = {
            "num_envs": config.get("num_envs"),
            "chosen_intervention_mode": config.get("chosen_intervention_mode"),
            "travel_time": config.get("travel_time"),
            "fix_duration": config.get("fix_duration"),
            "resume_delay": config.get("resume_delay"),
            "add_reference_number": config.get("add_reference_number"),
            "simulated_tooling_count": config.get("add_reference_number"),
            "allowed_overlap_ratio": config.get("allowed_overlap_ratio"),
            "layout_source": config.get("layout_source"),
            "episode_success_requires_reset_cycles": config.get("episode_success_requires_reset_cycles"),
            "reuse_verified_seed": config.get("reuse_verified_seed"),
            "headless": config.get("headless"),
        }
    if "KPI_TARGETS" in targets or "TRT_CURRENT" in targets:
        trt = repository.get_current_trt()
        sources.append("data/trt/current_trt.json")
        structured["current_trt"] = {"trt_id": trt.get("trt_id"), "version": trt.get("version")}
        structured["kpi_targets"] = [
            {
                "line_id": line_id,
                "min_throughput_per_hour": (line.get("kpi") or {}).get("min_throughput_per_hour"),
                "deadline_minutes": (line.get("kpi") or {}).get("deadline_minutes"),
                "max_downtime_seconds": (line.get("kpi") or {}).get("max_downtime_seconds"),
                "priority": line.get("priority"),
                "goal": line.get("goal"),
                "abnormal_strategy": line.get("abnormal_strategy"),
            }
            for line_id, line in sorted((trt.get("lines") or {}).items())
            if isinstance(line, dict) and (not requested_line_ids or line_id in requested_line_ids)
        ]
    if "TASK_REQUIREMENT_TABLE" in targets or "SCENARIO_SPEC" in targets:
        spec_path = None
        if scenario_spec_id:
            candidate_path = repository.root / "outputs" / "scenario_specs" / f"{scenario_spec_id}.json"
            spec_path = candidate_path if candidate_path.exists() else None
        spec_path = spec_path or _latest_json_file("outputs/scenario_specs")
        if spec_path:
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                spec = {}
            sources.append(str(spec_path.relative_to(repository.root)))
            structured["scenario_spec"] = {
                "scenario_spec_id": spec.get("scenario_spec_id"),
                "trt_id": spec.get("trt_id"),
                "trt_version": spec.get("trt_version"),
                "simulation_scope": spec.get("simulation_scope"),
                "simulation_config": spec.get("simulation_config"),
            }
            requirement_rows = _task_requirement_table_from_spec(spec)
            if requested_line_ids:
                requirement_rows = [row for row in requirement_rows if row.get("line_id") in requested_line_ids]
            structured["task_requirement_table"] = requirement_rows
        else:
            structured["scenario_spec"] = {"error": "No ScenarioSpec JSON file was found."}
    if "RUN_ARTIFACT" in targets:
        run_artifact, source = _latest_run_artifact_summary(run_id)
        if source:
            sources.append(source)
        if requested_line_ids:
            for key in ("line_kpis", "container_completion_events"):
                rows = run_artifact.get(key)
                if isinstance(rows, list):
                    run_artifact[key] = [row for row in rows if row.get("line_id") in requested_line_ids]
        structured["run_artifact"] = run_artifact
    if "DEPLOYMENT_HISTORY" in targets:
        deployments_dir = repository.root / "data" / "deployments"
        records = sorted(deployments_dir.glob("deploy_*.json"), key=lambda item: item.stat().st_mtime, reverse=True) if deployments_dir.exists() else []
        sources.append("data/deployments/*.json")
        structured["deployment_history"] = []
        for record in records[:5]:
            try:
                payload = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            structured["deployment_history"].append(
                {
                    "deployment_id": payload.get("deployment_id"),
                    "scenario_spec_id": payload.get("scenario_spec_id"),
                    "trt_version": payload.get("trt_version"),
                    "decision": payload.get("decision"),
                    "created_at": payload.get("created_at") or payload.get("updated_at"),
                }
            )
    return {
        "status": "ANSWER_READY",
        "query_targets": sorted(targets),
        "line_ids": line_ids or [],
        "scenario_spec_id": scenario_spec_id,
        "run_id": run_id,
        "sources": sorted(set(sources)),
        "structured_answer": structured,
    }


def _fallback_config_answer_message(answer: dict[str, Any]) -> str:
    structured = answer.get("structured_answer") or {}
    targets = set(answer.get("query_targets") or [])
    if "LINE_STATE" in targets:
        rows = ["State record details:"]
        for line in (structured.get("line_state") or {}).get("lines") or []:
            ent = line.get("entanglement") or {}
            rows.extend(
                [
                    f"{line.get('line_id')}:",
                    f"- Mode: {line.get('mode')}",
                    f"- Current task: {line.get('current_task') or 'none'}",
                    f"- WIP count: {line.get('wip_count')}",
                    f"- Active tooling set: {line.get('active_set_id')}",
                    f"- Entanglement: {'detected' if ent.get('detected') else 'not detected'}",
                    f"- Locked resources: {line.get('locked_resources') or []}",
                    f"- Last exception: {line.get('last_exception')}",
                    f"- Robot: {line.get('robot_id')}",
                    f"- Workspace: {line.get('workspace_id')}",
                ]
            )
        return "\n".join(rows)
    if "TIME_ARRIVAL_MODEL" in targets:
        cfg = structured.get("time_arrival_model") or {}
        return "\n".join(
            [
                "Current Time-Arrival Model settings:",
                f"- Active simulated production lines: {cfg.get('num_envs')}",
                f"- Intervention mode: {cfg.get('chosen_intervention_mode')}",
                f"- Operator arrival time: {cfg.get('travel_time')} seconds",
                f"- Entanglement fix time: {cfg.get('fix_duration')} seconds",
                f"- Recovery/resume delay: {cfg.get('resume_delay')} seconds",
                f"- Simulated tooling count per production line: {cfg.get('simulated_tooling_count')}",
                f"- Allowed overlap ratio: {cfg.get('allowed_overlap_ratio')}",
                f"- Layout source: {cfg.get('layout_source')}",
                f"- Reset cycles required: {cfg.get('episode_success_requires_reset_cycles')}",
                f"- Reuse verified seed: {cfg.get('reuse_verified_seed')}",
            ]
        )
    if "KPI_TARGETS" in targets:
        trt = structured.get("current_trt") or {}
        rows = [f"Current KPI settings for TRT {trt.get('trt_id')} version {trt.get('version')}:"]
        for row in structured.get("kpi_targets") or []:
            deadline = row.get("deadline_minutes")
            downtime = row.get("max_downtime_seconds")
            rows.extend(
                [
                    "",
                    f"{row.get('line_id')}:",
                    f"- Minimum throughput: {row.get('min_throughput_per_hour')} tools/hour",
                    f"- Deadline: {deadline if deadline is not None else 'none configured'}",
                    f"- Maximum downtime: {downtime if downtime is not None else 'none configured'}",
                    f"- Priority: {row.get('priority')}",
                    f"- Goal: {row.get('goal')}",
                    f"- Abnormal strategy: {row.get('abnormal_strategy')}",
                ]
            )
        if answer.get("sources"):
            rows.extend(["", f"Source: {', '.join(answer.get('sources') or [])}"])
        return "\n".join(rows)
    if "TASK_REQUIREMENT_TABLE" in targets:
        rows = ["Latest task requirement table:"]
        for row in structured.get("task_requirement_table") or []:
            rows.append(
                f"- {row.get('line_id')}: target set {row.get('target_set_id')}, "
                f"required tools {row.get('required_tool_ids')}, selected tools {row.get('selected_tool_ids')}, "
                f"excluded tools {row.get('excluded_tool_ids')}, priority {row.get('manipulator_priority')}, "
                f"KPI {row.get('kpi_target')}."
            )
        return "\n".join(rows)
    return "I found the requested configuration data. Sources: " + ", ".join(answer.get("sources") or [])


def _format_config_query_answer(answer: dict[str, Any]) -> dict[str, Any]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["operator_message", "confidence", "sources_used", "follow_up_suggestions"],
        "properties": {
            "operator_message": {"type": "string"},
            "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
            "sources_used": {"type": "array", "items": {"type": "string"}},
            "follow_up_suggestions": {"type": "array", "items": {"type": "string"}},
        },
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You format source-backed production-line configuration answers for operators. "
                "Use only the supplied structured_answer values. Do not invent missing values. "
                "Use internal field names only when they are part of the requested details; otherwise use operator-friendly wording."
            ),
        },
        {"role": "user", "content": json.dumps(answer, sort_keys=True)},
    ]
    body = {
        "model": os.getenv("VLLM_MODEL", DEFAULT_VLLM_MODEL),
        "messages": messages,
        "max_tokens": 4000,
        "structured_outputs": {"json": schema},
    }
    try:
        raw = _post_json(os.getenv("VLLM_CHAT_COMPLETIONS_URL", DEFAULT_VLLM_CHAT_COMPLETIONS_URL), body, 30)
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", raw)
        formatted = json.loads(content) if isinstance(content, str) else content
        if isinstance(formatted, dict) and formatted.get("operator_message"):
            message = str(formatted.get("operator_message") or "")
            targets = set(answer.get("query_targets") or [])
            has_details = True
            if "LINE_STATE" in targets:
                first_line = next(iter(((answer.get("structured_answer") or {}).get("line_state") or {}).get("lines") or []), {})
                required_tokens = [str(first_line.get("line_id") or ""), str(first_line.get("mode") or ""), str(first_line.get("robot_id") or "")]
                has_details = all(token and token in message for token in required_tokens)
            elif "TIME_ARRIVAL_MODEL" in targets:
                cfg = (answer.get("structured_answer") or {}).get("time_arrival_model") or {}
                required_tokens = [str(cfg.get("travel_time")), str(cfg.get("fix_duration")), str(cfg.get("resume_delay")), str(cfg.get("simulated_tooling_count"))]
                has_details = all(token and token in message for token in required_tokens)
            elif "KPI_TARGETS" in targets:
                rows = (answer.get("structured_answer") or {}).get("kpi_targets") or []
                required_line_ids = [str(row.get("line_id") or "") for row in rows]
                required_labels = ["Minimum", "Deadline", "downtime", "Priority", "Goal", "Abnormal"]
                has_details = all(line_id and line_id in message for line_id in required_line_ids) and all(
                    label.lower() in message.lower() for label in required_labels
                )
            elif "TASK_REQUIREMENT_TABLE" in targets:
                rows = (answer.get("structured_answer") or {}).get("task_requirement_table") or []
                required_line_ids = [str(row.get("line_id") or "") for row in rows]
                has_details = all(line_id and line_id in message for line_id in required_line_ids) and "target" in message.lower()
            if not has_details:
                raise ValueError("Formatted config answer omitted required source-backed details.")
            return formatted
    except Exception:
        pass
    return {
        "operator_message": _fallback_config_answer_message(answer),
        "confidence": "MEDIUM",
        "sources_used": answer.get("sources") or [],
        "follow_up_suggestions": [],
    }


@app.post("/chat/config-query")
def post_chat_config_query(payload: dict[str, Any]) -> dict[str, Any]:
    answer = _build_config_query_answer(
        query_targets=list(payload.get("query_targets") or []),
        line_ids=list(payload.get("line_ids") or []),
        scenario_spec_id=payload.get("scenario_spec_id"),
        run_id=payload.get("run_id"),
        raw_chat_input=str(payload.get("raw_chat_input") or ""),
    )
    formatted = _format_config_query_answer(answer)
    return {**answer, "formatted_answer": formatted, "operator_message": formatted.get("operator_message")}


@app.post("/chat/dialogue-decision")
def post_chat_dialogue_decision(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "default")
    latest = str(payload.get("latest_user_message") or payload.get("raw_chat_input") or "")
    if latest.strip():
        _record_m12_event(
            "INTENT_CREATED",
            source_module="trt_core.api.chat.dialogue_decision",
            session_id=session_id,
            operator_id=payload.get("operator_id"),
            payload={"turn_type": "chat_message", "has_text": True},
        )
    session = load_chat_session(session_id, repository)
    if session.get("state") == "WAITING_FOR_M12_TEST_APPROVAL":
        decision, revision = normalize_m12_approval_decision(latest)
        pending = session.get("pending_m12_test") or {}
        if decision == "CANCEL_TESTING":
            cancel_m12_pending_test(_m12_pending_path())
            clear_chat_session(session_id, repository)
            return {
                "status": "CANCELLED",
                "dialogue_state": "M12_TESTING_CANCELLED",
                "operator_message": "Milestone 12 semi-manual testing was cancelled.",
                "payload": {"message": "Milestone 12 semi-manual testing was cancelled."},
                "errors": [],
            }
        if decision == "EDIT_TEST" and revision:
            pending["natural_language_trigger"] = revision
            pending["operator_approval_status"] = "PENDING"
            pending["approval_status"] = "PENDING"
            pending["edited_at_utc"] = now_utc() if "now_utc" in globals() else None
            save_chat_session(session_id, {"state": "WAITING_FOR_M12_TEST_APPROVAL", "pending_m12_test": pending}, repository)
            pending_path = _m12_pending_path()
            pending_path.write_text(json.dumps(pending, indent=2, sort_keys=True), encoding="utf-8")
            return {
                "status": "WAITING_FOR_M12_TEST_APPROVAL",
                "dialogue_state": "WAITING_FOR_M12_TEST_APPROVAL",
                "operator_message": pending.get("prompt") or "M12 test revised. Reply APPROVE_TEST to run it.",
                "pending_m12_test": pending,
                "errors": [],
            }
        if decision == "SKIP_TEST":
            skipped = skip_m12_pending_test(_m12_pending_path(), operator_id=pending.get("approved_by_operator_id"))
            save_chat_session(session_id, {"state": "WAITING_FOR_M12_TEST_APPROVAL", "pending_m12_test": skipped}, repository)
            return {
                "status": "SKIPPED",
                "dialogue_state": "WAITING_FOR_M12_TEST_APPROVAL",
                "operator_message": "M12 test skipped. No scenario was run.",
                "pending_m12_test": skipped,
                "errors": [],
            }
        if decision == "APPROVE_TEST":
            approved = approve_m12_pending_test(_m12_pending_path(), operator_id=pending.get("approved_by_operator_id"))
            save_chat_session(session_id, {"state": "WAITING_FOR_M12_TEST_RUNNING", "pending_m12_test": approved}, repository)
            result = run_m12_approved_pending_test(_m12_pending_path(), repository=repository)
            message = "\n".join(
                [
                    "M12 semi-manual test completed.",
                    "",
                    f"Test case: {result.get('test_case_id')}",
                    f"Status: {result.get('status')}",
                    f"ScenarioSpec: {result.get('scenario_spec_id')}",
                    f"RunArtifact: {result.get('run_id')}",
                    "",
                    "Parsed command arguments:",
                    "- --num_envs 2",
                    "- --chosen_intervention_mode immediate-stop",
                    "- --travel_time 2.5",
                    "- --fix_duration 6.5",
                    "- --resume_delay 2.5",
                    "- --add_reference_number 5",
                    "",
                    "Evidence summary:",
                    "- Not available unless the approved n8n/Isaac path produced an evidence summary.",
                    "",
                    "No deployment was performed because this was a Milestone 12 semi-manual comparison test.",
                    "",
                    "Reply NEXT_TEST to prepare the next test, RERUN_TEST to rerun this test, or CANCEL_TESTING to stop.",
                ]
            )
            save_chat_session(session_id, {"state": "WAITING_FOR_M12_TEST_APPROVAL", "pending_m12_test": approved, "last_m12_test_result": result}, repository)
            return {
                "status": result.get("status"),
                "dialogue_state": "M12_TEST_COMPLETED",
                "operator_message": message,
                "pending_m12_test": approved,
                "m12_test_result": result,
                "errors": [] if result.get("status") == "COMPLETED" else [result.get("error_code")],
            }
        return {
            "status": "WAITING_FOR_M12_TEST_APPROVAL",
            "dialogue_state": "WAITING_FOR_M12_TEST_APPROVAL",
            "operator_message": "Reply APPROVE_TEST to run this M12 test, SKIP_TEST to skip it, EDIT_TEST: <revision> to revise it, or CANCEL_TESTING to stop.",
            "pending_m12_test": pending,
            "errors": [],
        }
    messages, decision_input = _build_dialogue_decision_prompt(payload, session)
    body = {
        "model": os.getenv("VLLM_MODEL", DEFAULT_VLLM_MODEL),
        "messages": messages,
        "max_tokens": 4000,
        "structured_outputs": {"json": _dialogue_decision_schema()},
    }
    url = os.getenv("VLLM_CHAT_COMPLETIONS_URL", DEFAULT_VLLM_CHAT_COMPLETIONS_URL)
    semantic_attempts: list[dict[str, Any]] = []
    try:
        raw = _post_json(
            url,
            body,
            float(os.getenv("VLLM_DIALOGUE_DECISION_TIMEOUT_SECONDS", "30")),
        )
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", raw)
        decision = json.loads(content) if isinstance(content, str) else content
        if not isinstance(decision, dict):
            raise ValueError("Dialogue decision output must be a JSON object.")
        validation_errors = _dialogue_time_arrival_validation_errors(decision, decision_input)
        semantic_attempts.append(
            {
                "attempt": 1,
                "status": "VALID" if not validation_errors else "INVALID_DIAGNOSTIC",
                "errors": validation_errors,
                "classification_preserved": True,
            }
        )
        if validation_errors:
            decision["semantic_validation_status"] = "INVALID_DIAGNOSTIC"
            decision["semantic_validation_errors"] = validation_errors
        decision["llm_semantic_validation_attempts"] = semantic_attempts
    except (OSError, TimeoutError, urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
        decision = {
            "dialogue_state": "UNKNOWN",
            "turn_type": "UNKNOWN",
            "operator_message": f"I could not evaluate the dialogue state: {exc}",
            "normalized_request": None,
            "missing_or_unclear_items": ["dialogue_state"],
            "approval_decision": None,
            "deployment_decision": None,
            "llm_semantic_validation_attempts": semantic_attempts,
        }

    dialogue_state = str(decision.get("dialogue_state") or "UNKNOWN")
    turn_type = str(decision.get("turn_type") or _turn_type_for_dialogue_state(dialogue_state))
    if not decision.get("dialogue_state"):
        dialogue_state = _dialogue_state_for_turn(turn_type)
    if turn_type in {"TASK_REQUEST", "CLARIFICATION_VALUES"} and dialogue_state == "UNKNOWN":
        # Routing and extraction are separate decisions. An incomplete task payload may
        # need clarification, but it is still a task rather than an unknown chat turn.
        dialogue_state = "NEEDS_CLARIFICATION"
        decision["dialogue_state"] = dialogue_state
        decision.setdefault("routing_repairs", []).append(
            {
                "code": "TASK_CLASSIFICATION_PRESERVED",
                "reason": "Task routing was preserved independently of field extraction quality.",
            }
        )
    if turn_type == "CANCEL":
        dialogue_state = "CANCELLED"
    elif turn_type == "HELP":
        dialogue_state = "HELP"
    elif turn_type == "CONFIG_QUERY":
        dialogue_state = "CONFIG_QUERY"
    elif turn_type in {"APPROVAL_DECISION", "DEPLOYMENT_DECISION"}:
        dialogue_state = turn_type
    operator_message = _operator_facing_text(decision.get("operator_message") or "")
    normalized_request = decision.get("normalized_request") if isinstance(decision.get("normalized_request"), dict) else {}
    normalized_request = dict(normalized_request or {})
    sim_updates = normalized_request.get("simulation_config_updates")
    if isinstance(sim_updates, dict) and not any(value is not None for value in sim_updates.values()):
        normalized_request["simulation_config_updates"] = None
    request_types = _internal_request_types(normalized_request.get("request_types") or [])
    if normalized_request.get("kpi_updates") and "KPI_LIMIT_UPDATE" not in request_types:
        request_types.append("KPI_LIMIT_UPDATE")
    if normalized_request.get("target_set_id") and "TOOLING_POLICY_UPDATE" not in request_types:
        request_types.append("TOOLING_POLICY_UPDATE")
    if normalized_request.get("manipulator_priority") and "MANIPULATOR_PRIORITY_UPDATE" not in request_types:
        request_types.append("MANIPULATOR_PRIORITY_UPDATE")
    if normalized_request.get("simulation_config_updates") and "SIMULATION_CONFIG_UPDATE" not in request_types:
        request_types.append("SIMULATION_CONFIG_UPDATE")
    if request_types:
        normalized_request["request_types"] = request_types
    sim_updates_repair = normalized_request.get("simulation_config_updates")
    if (
        isinstance(sim_updates_repair, dict)
        and "ABNORMAL_STRATEGY_UPDATE" in request_types
        and not sim_updates_repair.get("chosen_intervention_mode")
    ):
        sim_updates_repair["chosen_intervention_mode"] = "immediate-stop"
        normalized_request["simulation_config_updates"] = sim_updates_repair
        decision.setdefault("deterministic_repairs", []).append(
            {
                "code": "ABNORMAL_STRATEGY_MODE_FILLED",
                "field": "simulation_config_updates.chosen_intervention_mode",
                "value": "immediate-stop",
                "reason": (
                    "The model classified the turn as an abnormal-strategy update but omitted "
                    "the corresponding intervention mode."
                ),
            }
        )
    sim_updates_for_scope = normalized_request.get("simulation_config_updates") or {}
    if (
        sim_updates_for_scope.get("num_envs") is not None
        and normalized_request.get("target_scope") != "ALL_LINES"
        and not normalized_request.get("target_lines")
    ):
        try:
            available_lines = sorted((repository.get_current_trt().get("lines") or {}).keys())
        except RepositoryError:
            available_lines = ["line_1", "line_2", "line_3", "line_4"]
        count = max(1, min(int(sim_updates_for_scope["num_envs"]), len(available_lines) or 1))
        normalized_request["target_scope"] = "MULTIPLE_LINES" if count > 1 else "SINGLE_LINE"
        normalized_request["target_lines"] = available_lines[:count]
    if normalized_request.get("manipulator_priority"):
        priority = dict(normalized_request["manipulator_priority"])
        priority.pop("scope", None)
        normalized_request["manipulator_priority"] = {
            "policy": priority.get("policy") or "REQUIRED_FIRST",
            "enabled": bool(priority.get("enabled", True)),
            "tie_breaker": "FCFS",
            "ordered_tool_ids": [],
            "ordered_normalized_types": [],
        }
    if normalized_request.get("target_scope") == "ALL_LINES" and normalized_request.get("target_lines"):
        normalized_request["target_scope"] = (
            "MULTIPLE_LINES" if len(normalized_request.get("target_lines") or []) > 1 else "SINGLE_LINE"
        )
    required_for_review = {
        "operator_id": normalized_request.get("operator_id"),
        "reason": normalized_request.get("reason"),
        "intent_text": normalized_request.get("intent_text"),
    }
    target_ready = normalized_request.get("target_scope") == "ALL_LINES" or bool(normalized_request.get("target_lines"))
    has_update = bool(
        normalized_request.get("request_types")
        or normalized_request.get("kpi_updates")
        or normalized_request.get("target_set_id")
        or normalized_request.get("manipulator_priority")
        or normalized_request.get("simulation_config_updates")
    )
    if dialogue_state == "READY_FOR_REVIEW" and (
        any(not value for value in required_for_review.values()) or not target_ready or not has_update
    ):
        missing = [
            name
            for name, value in required_for_review.items()
            if not value
        ]
        if not target_ready:
            missing.append("target_lines")
        if not has_update:
            missing.append("task_change")
        dialogue_state = "NEEDS_CLARIFICATION"
        decision["dialogue_state"] = dialogue_state
        if turn_type == "READY_FOR_REVIEW":
            turn_type = "TASK_REQUEST"
        decision["missing_or_unclear_items"] = missing
        operator_message = _operator_facing_text(
            decision.get("operator_message")
            or f"Before I can submit this for review, I still need: {', '.join(missing)}."
        )
    conversation = decision_input["conversation"]
    response: dict[str, Any] = {
        "dialogue_state": dialogue_state,
        "turn_type": turn_type,
        "operator_message": operator_message,
        "normalized_request": normalized_request,
        "query_targets": decision.get("query_targets") or [],
        "line_ids": decision.get("line_ids") or [],
        "scenario_spec_id": decision.get("scenario_spec_id"),
        "run_id": decision.get("run_id"),
        "missing_or_unclear_items": decision.get("missing_or_unclear_items") or [],
        "approval_decision": decision.get("approval_decision"),
        "deployment_decision": decision.get("deployment_decision"),
        "session_id": safe_session_id(session_id) if "safe_session_id" in globals() else session_id,
        "conversation": conversation,
        "llm_decision_raw": decision,
    }
    if dialogue_state == "HELP" or turn_type == "HELP":
        message = _chat_help_message()
        response.update(
            {
                "status": "HELP",
                "operator_message": message,
                "payload": {"message": message, "user_message": message},
                "context": {"session_id": session_id},
                "errors": [],
            }
        )
    elif dialogue_state == "CONFIG_QUERY" or turn_type == "CONFIG_QUERY":
        query_targets = decision.get("query_targets") or []
        answer = _build_config_query_answer(
            query_targets=query_targets,
            line_ids=decision.get("line_ids") or [],
            scenario_spec_id=decision.get("scenario_spec_id"),
            run_id=decision.get("run_id"),
            raw_chat_input=latest,
        )
        formatted = _format_config_query_answer(answer)
        message = _operator_facing_text(formatted.get("operator_message") or operator_message)
        response.update(
            {
                "status": "CONFIG_QUERY",
                "operator_message": message,
                "payload": {
                    "message": message,
                    "user_message": message,
                    "query_targets": query_targets,
                    "line_ids": decision.get("line_ids") or [],
                    "structured_answer": answer.get("structured_answer"),
                    "sources": answer.get("sources") or [],
                    "formatted_answer": formatted,
                },
                "context": {"session_id": session_id},
                "errors": [],
            }
        )
    elif dialogue_state == "READY_FOR_REVIEW":
        current_trt = repository.get_current_trt()
        request_types_for_dry_run = set(normalized_request.get("request_types") or [])
        dry_run_only = bool(
            normalized_request.get("dry_run_only") is True
            or decision.get("action") == "PROPOSE_DRY_RUN"
            or "DRY_RUN_ONLY" in request_types_for_dry_run
        )
        simulation_config_updates = dict(normalized_request.get("simulation_config_updates") or {})
        if dry_run_only:
            simulation_config_updates["dry_run_only"] = True
        candidate = {
            "patch_id": f"patch_{uuid4()}",
            "trt_id": current_trt.get("trt_id"),
            "base_version": current_trt.get("version"),
            "operator_id": normalized_request.get("operator_id"),
            "reason": normalized_request.get("reason"),
            "intent_text": _resolved_dialogue_intent_text(
                normalized_request,
                operator_message or decision_input["active_request"]["original_user_request"],
            ),
            "line_id": None,
            "action": "PROPOSE_PATCH",
            "goal": None,
            "priority": None,
            "allowed_instruments": None,
            "excluded_instruments": None,
            "selected_normalized_types": None,
            "excluded_normalized_types": None,
            "selected_tool_ids": None,
            "excluded_tool_ids": None,
            "required_tool_ids": None,
            "status": "DRAFT",
            "target_scope": normalized_request.get("target_scope"),
            "target_lines": normalized_request.get("target_lines") or [],
            "target_set_id": normalized_request.get("target_set_id"),
            "request_types": normalized_request.get("request_types") or [],
            "detected_request_types": normalized_request.get("request_types") or [],
            "manipulator_priority": normalized_request.get("manipulator_priority"),
            "simulation_config_updates": simulation_config_updates,
            "kpi_updates": normalized_request.get("kpi_updates") or {},
            "tooling_policy": None,
            "abnormal_strategy": None,
            "clarification_questions": [],
            "unsupported_terms": [],
            "dry_run_only": dry_run_only,
            "deployment_allowed_after_success": normalized_request.get("deployment_allowed_after_success"),
            "failure_action_hint": normalized_request.get("failure_action_hint"),
        }
        try:
            intent_patch = normalize_domain_candidate(
                candidate,
                current_trt,
                time_arrival_baseline=time_arrival_prompt_context(repository),
            )
            intent_patch["dry_run_only"] = bool(dry_run_only)
            if dry_run_only:
                intent_patch["deployment_allowed_after_success"] = False
                if normalized_request.get("failure_action_hint"):
                    intent_patch["failure_action_hint"] = normalized_request["failure_action_hint"]
            validation = validate_intent_patch(intent_patch, repository)
            status = "REVIEWED" if validation.get("status") == "ACCEPTED" else "NEEDS_REVISION"
            errors = [] if status == "REVIEWED" else list(validation.get("rejection_reasons") or [])
            response.update(
                {
                    "status": status,
                    "payload": {
                        "candidate_patch": intent_patch,
                        "validation_results": validation.get("validation_results"),
                        "rejection_reasons": validation.get("rejection_reasons") or [],
                        "message": intent_patch.get("message"),
                    },
                    "context": {
                        "session_id": safe_session_id(session_id) if "safe_session_id" in globals() else session_id,
                        "operator_id": intent_patch.get("operator_id"),
                        "reason": intent_patch.get("reason"),
                        "intent_text": intent_patch.get("intent_text"),
                        "affected_lines": intent_patch.get("affected_lines") or [],
                        "simulation_config_updates": intent_patch.get("simulation_config_updates") or {},
                    },
                    "errors": errors,
                }
            )
            save_chat_session(
                session_id,
                {
                    "state": "WAITING_FOR_APPROVAL_DECISION" if status == "REVIEWED" else "WAITING_FOR_CLARIFICATION",
                    "conversation": conversation,
                    "latest_dialogue_decision": decision,
                    "normalized_request": normalized_request,
                    "candidate_patch_summary": intent_patch,
                    "review_status": status,
                    "pending_intent": None,
                },
                repository,
            )
            _record_m12_event(
                "CANDIDATE_SUMMARY_CREATED",
                source_module="trt_core.api.chat.dialogue_decision",
                session_id=session_id,
                operator_id=normalized_request.get("operator_id"),
                trt_id=intent_patch.get("trt_id"),
                trt_version=intent_patch.get("base_version"),
                payload={"patch_id": intent_patch.get("patch_id"), "status": status},
            )
        except (ValueError, RepositoryError) as exc:
            operator_message = _operator_facing_text(str(exc))
            if operator_message and (not conversation or conversation[-1] != {"role": "assistant", "content": operator_message}):
                conversation.append({"role": "assistant", "content": operator_message})
            save_chat_session(
                session_id,
                {
                    "state": "WAITING_FOR_CLARIFICATION",
                    "conversation": conversation,
                    "latest_dialogue_decision": decision,
                    "normalized_request": normalized_request,
                    "pending_intent": None,
                },
                repository,
            )
            response.update(
                {
                    "dialogue_state": "NEEDS_CLARIFICATION",
                    "status": "NEEDS_CLARIFICATION",
                    "operator_message": operator_message,
                    "payload": {"message": operator_message, "user_message": operator_message},
                    "context": {"session_id": session_id},
                    "errors": [operator_message],
                }
            )
    elif dialogue_state == "NEEDS_CLARIFICATION":
        missing_items = set(response.get("missing_or_unclear_items") or [])
        required_field_only = bool(missing_items) and missing_items <= {"operator_id", "reason", "intent_text"}
        if turn_type in {"TASK_REQUEST", "CLARIFICATION_VALUES"} and required_field_only:
            response.update(
                {
                    "status": "NEEDS_CLARIFICATION",
                    "payload": {"message": operator_message, "user_message": operator_message},
                    "context": {"session_id": session_id},
                    "errors": [],
                }
            )
            return response
        if operator_message and (not conversation or conversation[-1] != {"role": "assistant", "content": operator_message}):
            conversation.append({"role": "assistant", "content": operator_message})
        save_chat_session(
            session_id,
            {
                "state": "WAITING_FOR_CLARIFICATION",
                "conversation": conversation,
                "latest_dialogue_decision": decision,
                "normalized_request": normalized_request,
                "pending_intent": None,
            },
            repository,
        )
        response.update(
            {
                "status": "NEEDS_CLARIFICATION",
                "payload": {"message": operator_message, "user_message": operator_message},
                "context": {"session_id": session_id},
                "errors": [],
            }
        )
    elif dialogue_state == "CANCELLED":
        clear_chat_session(session_id, repository)
        response.update({"status": "CANCELLED", "payload": {"message": operator_message or "Cancelled."}, "errors": []})
    else:
        response.update(
            {
                "status": dialogue_state,
                "payload": {"message": operator_message},
                "context": {"session_id": session_id},
                "errors": []
                if dialogue_state in {"APPROVAL_DECISION", "DEPLOYMENT_DECISION"} or turn_type == "SMALL_TALK"
                else [operator_message or "Dialogue state is unknown."],
            }
        )
    return response


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
    merge_debug: dict[str, Any] = {
        "session_id": session_id,
        "pending_state": session.get("state"),
        "clarification_type": pending.get("clarification_type"),
        "operator_reply": clarification_text,
        "deterministic_result": None,
        "vllm_fallback_called": False,
        "vllm_fallback_result": None,
        "final_resolved": False,
        "final_target_lines": None,
    }

    def log_merge_debug(result: dict[str, Any] | None = None) -> None:
        if result is not None:
            merge_debug["final_resolved"] = bool(result.get("resolved"))
            merge_debug["final_target_lines"] = result.get("target_lines") or result.get("final_target_lines")
        logger.warning("chat_session.merge_clarification %s", json.dumps(merge_debug, sort_keys=True, default=str))

    try:
        current_trt = repository.get_current_trt(trt_id)
        closed_choice = resolve_pending_priority_clarification(pending, clarification_text, current_trt)
        if closed_choice is not None:
            merge_debug["deterministic_result"] = {
                "resolved": closed_choice.get("resolved"),
                "selected_option": closed_choice.get("selected_option"),
                "error_code": closed_choice.get("error_code"),
                "target_lines": closed_choice.get("target_lines"),
            }
        if closed_choice is None:
            merge_debug["vllm_fallback_called"] = True
            closed_choice = resolve_priority_clarification_with_vllm(pending, clarification_text, current_trt)
            merge_debug["vllm_fallback_result"] = {
                "resolved": closed_choice.get("resolved") if closed_choice else None,
                "selected_option": closed_choice.get("selected_option") if closed_choice else None,
                "confidence": closed_choice.get("confidence") if closed_choice else None,
                "target_lines": closed_choice.get("target_lines") if closed_choice else None,
                "reason": closed_choice.get("reason") if closed_choice else None,
            }
        if closed_choice and closed_choice.get("error_code") == "CLARIFICATION_LOOP_DETECTED":
            result = {
                "session_id": session["session_id"],
                "state": session.get("state"),
                "pending_intent": pending,
                **closed_choice,
            }
            log_merge_debug(result)
            return result
        if closed_choice and closed_choice.get("selected_option") == "PRODUCTION_LINE_PRIORITY":
            result = {
                "session_id": session["session_id"],
                "state": session.get("state"),
                "pending_intent": pending,
                **merged,
                **closed_choice,
            }
            log_merge_debug(result)
            return result
        if closed_choice and closed_choice.get("selected_option") == "ROBOT_REQUIRED_FIRST":
            candidate = {
                "patch_id": str(pending.get("patch_id") or f"patch_{uuid4()}"),
                "trt_id": current_trt["trt_id"],
                "base_version": current_trt["version"],
                "operator_id": str(closed_choice.get("operator_id") or pending.get("operator_id") or ""),
                "intent_text": str(closed_choice["intent_text"]),
                "reason": str(closed_choice.get("reason") or pending.get("reason") or ""),
                "target_scope": closed_choice.get("target_scope"),
                "target_lines": closed_choice.get("target_lines"),
                "target_set_id": closed_choice.get("target_set_id"),
                "request_types": closed_choice.get("request_types"),
                "detected_request_types": closed_choice.get("detected_request_types"),
                "manipulator_priority": closed_choice.get("manipulator_priority"),
                "simulation_config_updates": closed_choice.get("simulation_config_updates") or {},
                "excluded_instruments": None,
                "status": "DRAFT",
            }
            intent_patch = normalize_domain_candidate(
                candidate,
                current_trt,
                time_arrival_baseline=time_arrival_prompt_context(repository),
            )
            manipulator_priority = None
            target_set_id = None
            for operation in intent_patch.get("operations", []):
                path = str(operation.get("path") or "")
                if path.endswith("/manipulator_priority"):
                    manipulator_priority = operation.get("value")
                elif path.endswith("/target_set_id"):
                    target_set_id = operation.get("value")
            result = {
                "session_id": session["session_id"],
                "state": session.get("state"),
                "pending_intent": pending,
                **merged,
                **closed_choice,
                "resolved": True,
                "intent_text": closed_choice["intent_text"],
                "request_types": intent_patch.get("request_types") or [],
                "target_lines": intent_patch.get("affected_lines") or closed_choice.get("target_lines") or [],
                "target_set_id": target_set_id or closed_choice.get("target_set_id"),
                "manipulator_priority": manipulator_priority or closed_choice.get("manipulator_priority"),
                "simulation_config_updates": intent_patch.get("simulation_config_updates") or {},
                "candidate_patch": intent_patch,
                "intent_patch": intent_patch,
            }
            log_merge_debug(result)
            return result
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
        intent_patch = normalize_domain_candidate(
            candidate,
            current_trt,
            time_arrival_baseline=time_arrival_prompt_context(repository),
        )
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        result = {
            "session_id": session["session_id"],
            "state": session.get("state"),
            "pending_intent": pending,
            **merged,
            "resolved": False,
            "error": str(exc),
        }
        log_merge_debug(result)
        return result

    manipulator_priority = None
    target_set_id = None
    for operation in intent_patch.get("operations", []):
        path = str(operation.get("path") or "")
        if path.endswith("/manipulator_priority"):
            manipulator_priority = operation.get("value")
        elif path.endswith("/target_set_id"):
            target_set_id = operation.get("value")

    result = {
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
    log_merge_debug(result)
    return result


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


def _regenerate_time_arrival_candidate(
    candidate: dict[str, Any],
    current_trt: dict[str, Any],
    initial_errors: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Ask Gemma to regenerate invalid requested simulation values; never rewrite them locally."""

    baseline = time_arrival_prompt_context(repository)
    _ = current_trt
    metadata_fields = {
        field: candidate.get(field)
        for field in (
            "patch_id",
            "trt_id",
            "base_version",
            "operator_id",
            "intent_text",
            "reason",
            "status",
        )
    }
    timing_baseline = {
        field: baseline.get(field)
        for field in ("travel_time", "fix_duration", "resume_delay")
    }
    context = {
        "operator_intent": candidate.get("intent_text"),
        "recorded_time_arrival_state": timing_baseline,
        "validation_errors": initial_errors,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Independently derive the simulation configuration explicitly requested by the operator. "
                "Interpret relative timing language using only the recorded state supplied in the user message, "
                "and preserve explicit absolute values exactly. Include explicitly requested intervention mode "
                "and allowed overlap ratio. Use exactly 'immediate-stop' or 'continue-until-arrival' for "
                "chosen_intervention_mode. For every relative timing phrase, first record your own derivation: "
                "operator arrival time maps to travel_time, entanglement fix time maps to fix_duration, recovery "
                "delay maps to resume_delay, production-line count maps to num_envs, and tooling per line maps to "
                "add_reference_number. "
                "copy the recorded baseline, identify ADD or SUBTRACT from the operator's wording, identify the "
                "requested delta exactly as written (including decimal fractions), calculate the result once, and "
                "copy that result into simulation_config_updates. The delta is the number in the operator's relative "
                "phrase, not a value inferred from the previously rejected candidate. "
                "Do not apply a delta twice. Do not invent fields that the operator did not request. Return an "
                "object containing time_arrival_derivations and simulation_config_updates with only simulation "
                "fields requested by the operator. A "
                "deterministic validator will accept or reject your values but will never replace them. "
                "Return JSON only."
            ),
        },
        {"role": "user", "content": json.dumps(context, sort_keys=True)},
    ]
    body = {
        "model": os.getenv("VLLM_MODEL", DEFAULT_VLLM_MODEL),
        "messages": messages,
        "max_tokens": int(os.getenv("VLLM_INTENT_REGENERATION_MAX_TOKENS", "6000")),
    }
    attempts: list[dict[str, Any]] = []
    errors = list(initial_errors)
    endpoint = os.getenv("VLLM_CHAT_COMPLETIONS_URL", DEFAULT_VLLM_CHAT_COMPLETIONS_URL)
    max_attempts = max(1, int(os.getenv("VLLM_INTENT_SEMANTIC_ATTEMPTS", "3")))
    last_updates: dict[str, Any] = {}
    last_derivations: list[dict[str, Any]] = []
    explicit_fields = set(
        explicit_simulation_config_expectations(str(candidate.get("intent_text") or ""))
    )
    requested_fields = {
        field
        for field in (
            "travel_time",
            "fix_duration",
            "resume_delay",
            "chosen_intervention_mode",
            "allowed_overlap_ratio",
        )
        if field in explicit_fields or any(field in error for error in initial_errors)
    }
    for attempt_number in range(1, max_attempts + 1):
        raw = _post_json(
            endpoint,
            body,
            float(os.getenv("VLLM_INTENT_GENERATION_TIMEOUT_SECONDS", "120")),
        )
        choice = raw.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", raw)
        try:
            extracted = _parse_json_object_content(content)
            derivations = extracted.get("time_arrival_derivations")
            last_derivations = (
                [item for item in derivations if isinstance(item, dict)]
                if isinstance(derivations, list)
                else []
            )
            model_time_updates = extracted.get("simulation_config_updates")
            if not isinstance(model_time_updates, dict):
                model_time_updates = {
                    field: extracted[field]
                    for field in (
                        "travel_time",
                        "fix_duration",
                        "resume_delay",
                        "chosen_intervention_mode",
                        "allowed_overlap_ratio",
                    )
                    if field in extracted
                }
            if requested_fields:
                model_time_updates = {
                    field: value
                    for field, value in model_time_updates.items()
                    if field in requested_fields
                }
            regenerated_updates = {
                **(candidate.get("simulation_config_updates") or {}),
                **model_time_updates,
            }
            regenerated_sub_requests = []
            for sub_request in candidate.get("sub_requests") or []:
                if not isinstance(sub_request, dict):
                    regenerated_sub_requests.append(sub_request)
                    continue
                request_types = set(
                    sub_request.get("request_types")
                    or [sub_request.get("request_type")]
                )
                if (
                    "SIMULATION_CONFIG_UPDATE" in request_types
                    or sub_request.get("simulation_config_updates")
                ):
                    sub_request = {
                        **sub_request,
                        "simulation_config_updates": {
                            **(sub_request.get("simulation_config_updates") or {}),
                            **model_time_updates,
                        },
                    }
                regenerated_sub_requests.append(sub_request)
            regenerated = {
                **candidate,
                "simulation_config_updates": regenerated_updates,
                "sub_requests": regenerated_sub_requests or candidate.get("sub_requests"),
                **metadata_fields,
            }
            last_updates = dict(regenerated.get("simulation_config_updates") or {})
            errors = relative_time_arrival_validation_errors(regenerated, baseline)
            attempts.append(
                {
                    "attempt": attempt_number,
                    "status": "VALID" if not errors else "INVALID",
                    "errors": errors,
                    "model_simulation_config_updates": last_updates,
                    "model_time_arrival_derivations": last_derivations,
                    "reasoning_feature_used": bool(
                        raw.get("choices", [{}])[0].get("message", {}).get("reasoning")
                    ),
                    "usage": raw.get("usage"),
                }
            )
            if not errors:
                return regenerated, attempts
        except (ValueError, json.JSONDecodeError) as exc:
            errors = [f"Gemma regeneration JSON could not be validated: {exc}"]
            attempts.append(
                {
                    "attempt": attempt_number,
                    "status": "INVALID",
                    "errors": errors,
                    "model_simulation_config_updates": last_updates,
                    "model_time_arrival_derivations": last_derivations,
                    "finish_reason": choice.get("finish_reason"),
                    "content_type": type(content).__name__,
                    "reasoning_feature_used": bool(message.get("reasoning")),
                    "usage": raw.get("usage"),
                }
            )
        body["messages"] = [
            *messages,
            {
                "role": "user",
                "content": (
                    "The regenerated JSON still failed semantic validation: "
                    + "; ".join(errors)
                    + ". Re-evaluate the operator intent and recorded state, then return the complete JSON."
                ),
            },
        ]
    raise ValueError(
        "Gemma could not derive valid requested simulation values after "
        f"{max_attempts} regeneration attempts: "
        + "; ".join(errors)
        + f". Last model simulation_config_updates: {json.dumps(last_updates, sort_keys=True)}"
        + f". Last model derivations: {json.dumps(last_derivations, sort_keys=True)}"
    )


@app.post("/intent/normalize")
def post_intent_normalize(candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        logger.info("raw_llm_candidate=%r", candidate)
        logger.info("raw_llm_candidate.tooling_policy=%r", candidate.get("tooling_policy"))
        logger.info("request_body_sent_to_python.tooling_policy=%r", candidate.get("tooling_policy"))
        current_trt = repository.get_current_trt(candidate.get("trt_id"))
        baseline = time_arrival_prompt_context(repository)
        semantic_errors = relative_time_arrival_validation_errors(candidate, baseline)
        semantic_regeneration: list[dict[str, Any]] = []
        explicit_values = explicit_simulation_config_expectations(
            str(candidate.get("intent_text") or "")
        )
        if semantic_errors and explicit_values:
            candidate = deepcopy(candidate)
            candidate["simulation_config_updates"] = {
                **(candidate.get("simulation_config_updates") or {}),
                **explicit_values,
            }
            repaired_sub_requests = []
            for sub_request in candidate.get("sub_requests") or []:
                if not isinstance(sub_request, dict):
                    repaired_sub_requests.append(sub_request)
                    continue
                request_types = set(
                    sub_request.get("request_types")
                    or [sub_request.get("request_type")]
                )
                if "SIMULATION_CONFIG_UPDATE" in request_types:
                    sub_request = {
                        **sub_request,
                        "simulation_config_updates": {
                            **(sub_request.get("simulation_config_updates") or {}),
                            **explicit_values,
                        },
                    }
                repaired_sub_requests.append(sub_request)
            if repaired_sub_requests:
                candidate["sub_requests"] = repaired_sub_requests
            remaining_errors = relative_time_arrival_validation_errors(candidate, baseline)
            semantic_regeneration.append(
                {
                    "attempt": 0,
                    "status": "VALID" if not remaining_errors else "INVALID",
                    "method": "DETERMINISTIC_EXPLICIT_VALUE_RECOVERY",
                    "errors_before": semantic_errors,
                    "errors": remaining_errors,
                    "recovered_simulation_config_updates": explicit_values,
                    "reasoning_feature_used": False,
                }
            )
            semantic_errors = remaining_errors
        if semantic_errors:
            candidate, model_regeneration = _regenerate_time_arrival_candidate(
                candidate,
                current_trt,
                semantic_errors,
            )
            semantic_regeneration.extend(model_regeneration)
        intent_patch = normalize_domain_candidate(
            candidate,
            current_trt,
            time_arrival_baseline=baseline,
        )
        logger.info("normalize_domain_candidate.intent_patch.operations=%r", intent_patch.get("operations"))
        return {
            "intent_patch": intent_patch,
            "llm_semantic_regeneration": semantic_regeneration,
        }
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
        result = record_release_decision(decision_request, repository)
        _record_m12_event(
            "CANDIDATE_REVIEW_ENDED",
            source_module="trt_core.api.release.decision",
            operator_id=decision_request.get("operator_id"),
            trt_id=result.get("trt_id"),
            trt_version=result.get("trt_version"),
            payload={
                "release_id": decision_request.get("release_id"),
                "decision": decision_request.get("decision"),
                "status": result.get("status"),
            },
        )
        return result
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


@app.get("/state/time-arrival")
def get_state_time_arrival() -> dict[str, Any]:
    try:
        return load_time_arrival_state(repository)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Time-Arrival state is invalid: {exc}") from exc


@app.put("/state/time-arrival")
def put_state_time_arrival(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return save_time_arrival_state(
            payload,
            repository=repository,
            source=str(payload.get("source") or "MANUAL_STATE_UPDATE"),
            source_reference=payload.get("source_reference"),
            run_id=payload.get("run_id"),
            scenario_spec_id=payload.get("scenario_spec_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    simulation_override = payload.get("simulation_config_updates") or payload.get("simulation_config_override") or {}
    if (
        isinstance(simulation_override, dict)
        and simulation_override.get("num_envs") is not None
        and not payload.get("simulation_scope")
    ):
        registry = load_line_registry(repository)
        enabled_line_ids = sorted(line_id for line_id, line in registry["lines"].items() if line.get("enabled") is True)
        affected_lines = [line_id for line_id in (payload.get("affected_lines") or []) if line_id in enabled_line_ids]
        count = max(1, min(int(simulation_override["num_envs"]), len(enabled_line_ids) or 1))
        lines = affected_lines[:count] if len(affected_lines) >= count else enabled_line_ids[:count]
        payload = {
            **payload,
            "simulation_scope": {
                "mode": "EXPLICIT_OPERATOR_LIMITED",
                "lines": lines,
                "reason": "Operator requested a reduced-line dry run or limited simulation.",
            },
        }
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
        if affected_lines:
            reconciliation_plan["affected_lines"] = affected_lines
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
            strategy_batch_id=payload.get("strategy_batch_id"),
            output_path=repository.root / "outputs",
            template_registry=template_registry,
            include_waiting_scenarios=bool(payload.get("include_waiting_scenarios", False)),
            line_bindings=scenario_resolution["line_bindings"],
            required_line_ids=scenario_resolution["required_lines"],
            simulation_scope=scenario_resolution["simulation_scope"],
            simulation_config_override=payload.get("simulation_config_updates") or payload.get("simulation_config_override"),
            line_policy_overrides=payload.get("line_policy_overrides"),
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
    _record_m12_event(
        "SCENARIO_CREATED",
        source_module="trt_core.api.scenario.generate",
        scenario_spec_id=result.get("scenario_spec_id"),
        trt_id=result.get("trt_id"),
        trt_version=result.get("trt_version"),
        payload={
            "scenario_spec_path": result.get("workspace_contract", {}).get("expected_scenario_spec_path"),
            "release_id": payload.get("release_id"),
            "reconciliation_plan_id": payload.get("reconciliation_plan_id"),
        },
    )
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
    _record_m12_event(
        "SIMULATION_STARTED",
        source_module="trt_core.api.simulation.run",
        run_id=run_id,
        scenario_spec_id=command_info["scenario_spec_id"],
        trt_id=scenario_spec.get("trt_id"),
        trt_version=scenario_spec.get("trt_version"),
        payload={"run_mode": run_mode, "output_db_path": command_info["output_db_path"]},
    )

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
    _record_m12_event(
        "RUN_ARTIFACT_CREATED",
        source_module="trt_core.api.simulation.run",
        run_id=run_id,
        scenario_spec_id=command_info["scenario_spec_id"],
        trt_id=scenario_spec.get("trt_id"),
        trt_version=scenario_spec.get("trt_version"),
        payload={
            "status": status,
            "error_code": error_code,
            "output_db_path": command_info["output_db_path"],
            "host_timing": host_result.get("timing"),
        },
    )
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
    _record_m12_event(
        "RUN_ARTIFACT_CREATED",
        source_module="trt_core.api.simulation.poll",
        run_id=run_id,
        scenario_spec_id=command_info["scenario_spec_id"],
        trt_id=scenario_spec.get("trt_id"),
        trt_version=scenario_spec.get("trt_version"),
        payload={
            "status": status,
            "error_code": error_code,
            "output_db_path": command_info["output_db_path"],
            "host_timing": host_result.get("timing"),
        },
    )
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


@app.post("/evidence/summarize")
def post_evidence_summarize(payload: dict[str, Any]) -> dict[str, Any]:
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        raise HTTPException(status_code=422, detail="run_id is required")
    scenario_spec_id = payload.get("scenario_spec_id")
    trt_id = str(payload.get("trt_id") or "trt-demo")
    trt_version = payload.get("trt_version")
    host_runner = payload.get("host_runner") if isinstance(payload.get("host_runner"), dict) else None
    evidence = build_evidence_summary(
        repository=repository,
        run_id=run_id,
        scenario_spec_id=scenario_spec_id,
        trt_id=trt_id,
        trt_version=trt_version,
        scenario_spec_path=payload.get("scenario_spec_path"),
        output_db_path=payload.get("output_db_path"),
        host_runner=host_runner,
        source_seed_sweep_db_path=payload.get("source_seed_sweep_db_path"),
    )
    summary = evidence.get("evidence_summary") or {}
    if payload.get("session_id") and summary.get("next_action") in {"ASK_DEPLOY_APPROVAL", "ASK_DEPLOY_ACKNOWLEDGEMENT"}:
        raw_artifact = (evidence.get("raw_evidence") or {}).get("run_artifact") or {}
        session_payload = {
            "state": "WAITING_FOR_DEPLOYMENT_DECISION",
            "pending_intent": None,
            "pending_deployment": {
                "run_id": run_id,
                "scenario_spec_id": evidence.get("scenario_spec_id"),
                "trt_id": raw_artifact.get("trt_id") or trt_id,
                "trt_version": raw_artifact.get("trt_version") or trt_version,
                "evidence_summary_id": f"evidence_{uuid4()}",
                "deployment_recommended": bool(summary.get("deployment_recommended")),
                "deployment_allowed": bool(summary.get("deployment_allowed")),
                "requires_operator_acknowledgement": bool(summary.get("requires_operator_acknowledgement")),
                "risk_tier": summary.get("risk_tier"),
                "acknowledged_risks": summary.get("acknowledged_risks") or [],
                "operator_options": summary.get("operator_options") or [],
                "evidence_summary": summary,
            },
        }
        save_chat_session(str(payload["session_id"]), session_payload, repository)
    elif payload.get("session_id") and summary.get("next_action") in {
        "REVISE_OR_RERUN",
        "REQUEST_REVISION_OR_RERUN",
        "WAIT",
    }:
        raw_artifact = (evidence.get("raw_evidence") or {}).get("run_artifact") or {}
        save_chat_session(
            str(payload["session_id"]),
            {
                "state": "WAITING_FOR_POST_EVIDENCE_DECISION",
                "pending_intent": None,
                "allowed_actions": ["REQUEST_REVISION", "RERUN_SIMULATION", "CANCEL"],
                "pending_evidence": {
                    "run_id": run_id,
                    "scenario_spec_id": evidence.get("scenario_spec_id") or scenario_spec_id,
                    "trt_id": raw_artifact.get("trt_id") or trt_id,
                    "trt_version": raw_artifact.get("trt_version") or trt_version,
                    "evidence_summary": summary,
                },
            },
            repository,
        )
    return evidence


def _run_strategy_batch_worker(strategy_batch_id: str) -> None:
    with STRATEGY_SIMULATION_LOCK:
        batch = repository.load_strategy_batch(strategy_batch_id)
        batch["status"] = "RUNNING"
        batch["started_at_utc"] = strategy_now_utc()
        batch["updated_at_utc"] = batch["started_at_utc"]
        repository.save_strategy_batch(batch)
        execution = batch["execution_request"]
        candidate_runs: list[dict[str, Any]] = []
        try:
            for candidate in batch["candidates"]:
                candidate_id = candidate["candidate_strategy_id"]
                candidate_clock = time.perf_counter()
                record: dict[str, Any] = {
                    "candidate_strategy_id": candidate_id,
                    "status": "GENERATING_SCENARIO",
                    "started_at_utc": strategy_now_utc(),
                }
                candidate_runs.append(record)
                batch["candidate_runs"] = candidate_runs
                batch["updated_at_utc"] = strategy_now_utc()
                repository.save_strategy_batch(batch)
                try:
                    scenario_clock = time.perf_counter()
                    scenario_result = post_scenario_generate(
                        {
                            **execution,
                            "candidate_strategy_id": candidate_id,
                            "strategy_batch_id": strategy_batch_id,
                            "line_policy_overrides": candidate.get("line_policy_overrides") or {},
                            "simulation_config_updates": merge_candidate_simulation_config(batch, candidate),
                        }
                    )
                    if not isinstance(scenario_result, dict) or scenario_result.get("status") != "GENERATED":
                        rejection_detail = None
                        if isinstance(scenario_result, dict):
                            rejection_detail = (
                                scenario_result.get("rejection_reason")
                                or scenario_result.get("detail")
                                or (scenario_result.get("payload") or {}).get("message")
                                or "; ".join(str(item) for item in (scenario_result.get("errors") or []))
                            )
                        raise RuntimeError(
                            f"Scenario generation did not complete: "
                            f"{scenario_result.get('status') if isinstance(scenario_result, dict) else type(scenario_result).__name__}"
                            f"{f': {rejection_detail}' if rejection_detail else ''}"
                        )
                    scenario_spec = scenario_result["scenario_spec"]
                    specification_seconds = time.perf_counter() - scenario_clock
                    record.update(
                        {
                            "status": "SIMULATING",
                            "scenario_spec_id": scenario_result["scenario_spec_id"],
                            "scenario_spec_path": scenario_result["scenario_spec_path"],
                            "scenario_created_at_utc": strategy_now_utc(),
                        }
                    )
                    repository.save_strategy_batch({**batch, "candidate_runs": candidate_runs, "updated_at_utc": strategy_now_utc()})
                    simulation_clock = time.perf_counter()
                    simulation = post_simulation_run(
                        {
                            "scenario_spec_id": scenario_result["scenario_spec_id"],
                            "scenario_spec_path": scenario_result["scenario_spec_path"],
                            "run_mode": "SYNC",
                            "headless": execution.get("headless", False),
                            "timeout_seconds": execution.get("timeout_seconds"),
                        }
                    )
                    simulation_wall_seconds = time.perf_counter() - simulation_clock
                    if not isinstance(simulation, dict):
                        raise RuntimeError("Simulation endpoint returned a non-object response.")
                    host_timing = ((simulation.get("host_runner") or {}).get("timing") or {})
                    simulation_startup_seconds = host_timing.get("isaac_startup_seconds")
                    strategy_verification_seconds = (
                        max(0.0, simulation_wall_seconds - float(simulation_startup_seconds))
                        if isinstance(simulation_startup_seconds, (int, float))
                        else None
                    )
                    timing_scope_status = (
                        "ISAAC_STARTUP_EXCLUDED_USING_" + str(host_timing.get("startup_reference_source"))
                        if strategy_verification_seconds is not None
                        else "DATA_INCOMPLETE_STARTUP_BOUNDARY_NOT_MEASURED"
                    )
                    record.update(
                        {
                            "run_id": simulation.get("run_id"),
                            "simulation_status": simulation.get("status"),
                            "simulation_error_code": simulation.get("error_code"),
                            "simulation_errors": simulation.get("errors") or [],
                            "artifact_created_at_utc": strategy_now_utc(),
                            "timing": {
                                "llm_generation_seconds": elapsed_seconds(
                                    (batch.get("generation_provenance") or {}).get("started_at_utc"),
                                    (batch.get("generation_provenance") or {}).get("completed_at_utc"),
                                ),
                                "specification_parsing_seconds": specification_seconds,
                                "environment_wait_seconds": elapsed_seconds(
                                    batch.get("queued_at_utc"),
                                    record.get("started_at_utc"),
                                ),
                                "simulation_startup_seconds": simulation_startup_seconds,
                                "simulation_startup_and_execution_seconds": simulation_wall_seconds,
                                "verification_excluding_startup_seconds": strategy_verification_seconds,
                                "isaac_command_started_at_utc": host_timing.get("isaac_command_started_at_utc"),
                                "isaac_startup_reference_at_utc": host_timing.get("startup_reference_at_utc"),
                                "isaac_startup_reference_pattern": host_timing.get("startup_reference_pattern"),
                                "isaac_startup_reference_source": host_timing.get("startup_reference_source"),
                                "reset_seconds": None,
                                "strategy_simulation_seconds": None,
                                "kpi_calculation_seconds": None,
                                "automated_verification_seconds": None,
                                "manual_review_seconds": None,
                                "end_to_end_seconds": None,
                                "timing_scope_status": timing_scope_status,
                            },
                        }
                    )
                    run_artifact = simulation.get("run_artifact")
                    if simulation.get("status") != "COMPLETED" or not isinstance(run_artifact, dict):
                        record.update(
                            {
                                "status": "FAILED",
                                "measurements": {
                                    "eligible": False,
                                    "blocking_reasons": ["SIMULATION_NOT_COMPLETED"],
                                    "data_quality_status": "DATA_INCOMPLETE",
                                },
                            }
                        )
                        continue
                    evidence_clock = time.perf_counter()
                    evidence = build_evidence_summary(
                        repository=repository,
                        run_id=str(simulation["run_id"]),
                        scenario_spec_id=scenario_result["scenario_spec_id"],
                        trt_id=batch["trt_id"],
                        trt_version=batch["trt_version"],
                        scenario_spec_path=scenario_result["scenario_spec_path"],
                        output_db_path=simulation.get("output_db_path"),
                        host_runner=simulation.get("host_runner"),
                    )
                    kpi_and_evidence_seconds = time.perf_counter() - evidence_clock
                    evidence_summary = evidence.get("evidence_summary") or {}
                    verification_clock = time.perf_counter()
                    measurements = candidate_measurements(
                        run_artifact=run_artifact,
                        scenario_spec=scenario_spec,
                        evidence_summary=evidence_summary,
                    )
                    verification_seconds = time.perf_counter() - verification_clock
                    record.update(
                        {
                            "status": "EVALUATED",
                            "evidence_created_at_utc": strategy_now_utc(),
                            "evidence_summary": evidence_summary,
                            "measurements": measurements,
                            "timing": {
                                "llm_generation_seconds": elapsed_seconds(
                                    (batch.get("generation_provenance") or {}).get("started_at_utc"),
                                    (batch.get("generation_provenance") or {}).get("completed_at_utc"),
                                ),
                                "specification_parsing_seconds": specification_seconds,
                                "environment_wait_seconds": elapsed_seconds(
                                    batch.get("queued_at_utc"),
                                    record.get("started_at_utc"),
                                ),
                                "simulation_startup_seconds": simulation_startup_seconds,
                                "simulation_startup_and_execution_seconds": simulation_wall_seconds,
                                "verification_excluding_startup_seconds": strategy_verification_seconds,
                                "isaac_command_started_at_utc": host_timing.get("isaac_command_started_at_utc"),
                                "isaac_startup_reference_at_utc": host_timing.get("startup_reference_at_utc"),
                                "isaac_startup_reference_pattern": host_timing.get("startup_reference_pattern"),
                                "isaac_startup_reference_source": host_timing.get("startup_reference_source"),
                                "reset_seconds": None,
                                "strategy_simulation_seconds": measurements.get("strategy_simulation_seconds"),
                                "kpi_calculation_seconds": kpi_and_evidence_seconds,
                                "automated_verification_seconds": verification_seconds,
                                "manual_review_seconds": None,
                                "end_to_end_seconds": None,
                                "timing_scope_status": timing_scope_status,
                            },
                        }
                    )
                except Exception as exc:
                    logger.exception(
                        "strategy_batch.candidate_failed batch_id=%s candidate_id=%s",
                        strategy_batch_id,
                        candidate_id,
                    )
                    record.update(
                        {
                            "status": "SYSTEM_ERROR",
                            "error": str(exc),
                            "measurements": {
                                "eligible": False,
                                "blocking_reasons": ["SYSTEM_ERROR"],
                                "data_quality_status": "DATA_INCOMPLETE",
                            },
                        }
                    )
                finally:
                    record["completed_at_utc"] = strategy_now_utc()
                    record["candidate_worker_wall_seconds"] = time.perf_counter() - candidate_clock
                    batch["candidate_runs"] = candidate_runs
                    batch["updated_at_utc"] = record["completed_at_utc"]
                    repository.save_strategy_batch(batch)
            batch["selection"] = rank_candidate_runs(candidate_runs)
            batch["status"] = batch["selection"]["status"]
        except Exception as exc:
            logger.exception("strategy_batch.failed batch_id=%s", strategy_batch_id)
            batch["status"] = "SYSTEM_ERROR"
            batch["error"] = str(exc)
        finally:
            batch["completed_at_utc"] = strategy_now_utc()
            batch["updated_at_utc"] = batch["completed_at_utc"]
            repository.save_strategy_batch(batch)


@app.post("/strategy/candidates/generate")
def post_strategy_candidates_generate(payload: dict[str, Any]) -> dict[str, Any]:
    required = ["release_id", "trt_id", "trt_version", "reconciliation_plan_id"]
    missing = [field for field in required if not payload.get(field)]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing strategy generation fields: {', '.join(missing)}")
    try:
        released_trt = repository.load_trt(str(payload["trt_id"]), str(payload["trt_version"]))
        released_trt["release_id"] = payload["release_id"]
        release_record = repository.load_release_record(str(payload["release_id"]))
        if release_record.get("status") != "RELEASED":
            raise ValueError(
                f"Candidate generation requires a RELEASED record; "
                f"{payload['release_id']} is {release_record.get('status')}."
            )
        if (
            release_record.get("trt_id") != payload["trt_id"]
            or release_record.get("trt_version") != payload["trt_version"]
        ):
            raise ValueError(
                "Release TRT identity/version does not match the candidate-generation request."
            )
        plan = repository.load_reconciliation_plan(str(payload["reconciliation_plan_id"]))
        _validate_scenario_reconciliation_contract(payload, plan)
        requested_candidate_count = int(payload.get("candidate_count") or 3)
        simulation_updates = payload.get("simulation_config_updates") or {}
        explicit_scope = payload.get("simulation_scope") or {}
        simulation_line_ids = [
            line_id
            for line_id in (explicit_scope.get("lines") or [])
            if line_id in (released_trt.get("lines") or {})
        ]
        if not simulation_line_ids and simulation_updates.get("num_envs") is not None:
            available_line_ids = sorted((released_trt.get("lines") or {}).keys())
            affected_line_ids = [
                line_id
                for line_id in (payload.get("affected_lines") or plan.get("affected_lines") or [])
                if line_id in available_line_ids
            ]
            count = max(
                1,
                min(int(simulation_updates["num_envs"]), len(available_line_ids) or 1),
            )
            simulation_line_ids = (
                affected_line_ids[:count]
                if len(affected_line_ids) >= count
                else available_line_ids[:count]
            )
        try:
            batch = generate_candidate_batch(
                repository=repository,
                released_trt=released_trt,
                reconciliation_plan=plan,
                state_records=load_current_state(repository),
                candidate_count=requested_candidate_count,
                locked_simulation_config=payload.get("simulation_config_updates") or {},
                locked_line_policy_fields=locked_line_policy_fields_from_release(
                    release_record,
                    released_trt,
                ),
                simulation_line_ids=simulation_line_ids or None,
            )
        except ValueError as exc:
            timestamp = strategy_now_utc()
            batch = {
                "strategy_batch_id": f"strategy_batch_{uuid4()}",
                "status": "GENERATION_FAILED",
                "trt_id": payload["trt_id"],
                "trt_version": payload["trt_version"],
                "reconciliation_plan_id": payload["reconciliation_plan_id"],
                "candidate_count": requested_candidate_count,
                "candidates": [],
                "candidate_runs": [],
                "selection": {
                    "status": "GENERATION_FAILED",
                    "selected_candidate_strategy_id": None,
                    "selected_scenario_spec_id": None,
                    "selected_run_id": None,
                    "ranked_candidates": [],
                    "operator_refinement_required": False,
                    "operator_intent_fault_detected": False,
                    "automatic_retry_exhausted": True,
                    "failure_classification": "SYSTEM_GENERATION_ERROR",
                    "refinement_suggestions": [],
                    "recovery_actions": [
                        "Retry candidate generation using the unchanged operator intent.",
                        "Inspect the model output and deterministic validation errors if retrying fails.",
                    ],
                },
                "failure_stage": "CANDIDATE_GENERATION",
                "failure_source": "LLM_CANDIDATE_OUTPUT_VALIDATION",
                "generation_error": str(exc),
                "created_at_utc": timestamp,
                "updated_at_utc": timestamp,
                "completed_at_utc": timestamp,
            }
        batch["execution_request"] = {
            "release_id": payload["release_id"],
            "trt_id": payload["trt_id"],
            "trt_version": payload["trt_version"],
            "reconciliation_plan_id": payload["reconciliation_plan_id"],
            "scenario_template_id": payload.get("scenario_template_id"),
            "affected_lines": payload.get("affected_lines") or plan.get("affected_lines") or [],
            "line_decisions": payload.get("line_decisions") or plan.get("line_decisions") or [],
            "simulation_scope": payload.get("simulation_scope"),
            "include_waiting_scenarios": bool(payload.get("include_waiting_scenarios", False)),
            "headless": bool(payload.get("headless", False)),
            "timeout_seconds": payload.get("timeout_seconds"),
        }
        repository.save_strategy_batch(batch)
        return batch
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, ScenarioGenerationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/strategy/batches/{strategy_batch_id}/run")
def post_strategy_batch_run(strategy_batch_id: str) -> dict[str, Any]:
    try:
        batch = repository.load_strategy_batch(strategy_batch_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    worker = STRATEGY_BATCH_WORKERS.get(strategy_batch_id)
    if worker and worker.is_alive():
        return {
            "status": "RUNNING",
            "strategy_batch_id": strategy_batch_id,
            "message": "Candidate simulations are already running sequentially.",
        }
    if batch.get("status") in {"SELECTED", "NO_ELIGIBLE_STRATEGY", "GENERATION_FAILED"}:
        return batch
    worker = threading.Thread(
        target=_run_strategy_batch_worker,
        args=(strategy_batch_id,),
        name=f"strategy-batch-{strategy_batch_id}",
        daemon=True,
    )
    batch["status"] = "QUEUED"
    batch["queued_at_utc"] = strategy_now_utc()
    batch["updated_at_utc"] = batch["queued_at_utc"]
    repository.save_strategy_batch(batch)
    STRATEGY_BATCH_WORKERS[strategy_batch_id] = worker
    worker.start()
    return {
        "status": "QUEUED",
        "strategy_batch_id": strategy_batch_id,
        "candidate_count": batch["candidate_count"],
        "execution_policy": "STRICTLY_SEQUENTIAL_SINGLE_ISAAC_RUN",
        "poll_url": f"/strategy/batches/{strategy_batch_id}",
    }


@app.get("/strategy/batches/{strategy_batch_id}")
def get_strategy_batch(strategy_batch_id: str) -> dict[str, Any]:
    try:
        batch = repository.load_strategy_batch(strategy_batch_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if batch.get("status") in {"QUEUED", "RUNNING"}:
        worker = STRATEGY_BATCH_WORKERS.get(strategy_batch_id)
        if worker is None or not worker.is_alive():
            batch["status"] = "SYSTEM_ERROR"
            batch["error"] = (
                "Candidate simulation worker is no longer active. "
                "The API process may have restarted; submit the batch run again to rerun all candidates."
            )
            batch["updated_at_utc"] = strategy_now_utc()
            batch["completed_at_utc"] = batch["updated_at_utc"]
            repository.save_strategy_batch(batch)
    return batch


@app.get("/strategy/batches")
def get_strategy_batches() -> dict[str, Any]:
    return {"strategy_batches": repository.list_strategy_batches()}


@app.post("/deployment/simulated-deploy")
def post_deployment_simulated_deploy(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("M12_AUTOMATED_COMPARISON_TEST", "").lower() == "true":
        _record_m12_event(
            "DEPLOYMENT_BLOCKED",
            source_module="trt_core.api.deployment.simulated_deploy",
            run_id=str(payload.get("run_id") or ""),
            scenario_spec_id=str(payload.get("scenario_spec_id") or ""),
            trt_id=str(payload.get("trt_id") or ""),
            trt_version=str(payload.get("trt_version") or ""),
            operator_id=payload.get("operator_id"),
            payload={"reason": "Deployment disabled during M12 automated comparison testing."},
        )
        return {
            "status": "BLOCKED",
            "reason": "Deployment disabled during M12 automated comparison testing.",
            "deployment_allowed": False,
            "deployment_suppressed_reason": "M12 automated comparison test mode",
        }
    required = ["run_id", "scenario_spec_id", "trt_id", "trt_version"]
    missing = [field for field in required if not payload.get(field)]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required deployment fields: {', '.join(missing)}")
    _record_m12_event(
        "DEPLOYMENT_REVIEW_ENDED",
        source_module="trt_core.api.deployment.simulated_deploy",
        run_id=str(payload["run_id"]),
        scenario_spec_id=str(payload["scenario_spec_id"]),
        trt_id=str(payload["trt_id"]),
        trt_version=str(payload["trt_version"]),
        operator_id=payload.get("operator_id"),
        payload={"decision": payload.get("decision")},
    )
    _record_m12_event(
        "DEPLOYMENT_ATTEMPTED",
        source_module="trt_core.api.deployment.simulated_deploy",
        run_id=str(payload["run_id"]),
        scenario_spec_id=str(payload["scenario_spec_id"]),
        trt_id=str(payload["trt_id"]),
        trt_version=str(payload["trt_version"]),
        operator_id=payload.get("operator_id"),
        payload={"decision": payload.get("decision"), "force": bool(payload.get("force", False))},
    )
    try:
        result = simulated_deploy(
            repository=repository,
            run_id=str(payload["run_id"]),
            scenario_spec_id=str(payload["scenario_spec_id"]),
            trt_id=str(payload["trt_id"]),
            trt_version=str(payload["trt_version"]),
            operator_id=payload.get("operator_id"),
            deployment_comment=payload.get("deployment_comment"),
            decision=payload.get("decision"),
            acknowledged_risks=payload.get("acknowledged_risks") if isinstance(payload.get("acknowledged_risks"), list) else None,
            force=bool(payload.get("force", False)),
        )
        _record_m12_event(
            "DEPLOYMENT_COMPLETED" if result.get("status") == "DEPLOYED" else "DEPLOYMENT_BLOCKED",
            source_module="trt_core.api.deployment.simulated_deploy",
            run_id=str(payload["run_id"]),
            scenario_spec_id=str(payload["scenario_spec_id"]),
            trt_id=str(payload["trt_id"]),
            trt_version=str(payload["trt_version"]),
            operator_id=payload.get("operator_id"),
            payload={"status": result.get("status"), "deployment_id": result.get("deployment_id"), "errors": result.get("errors") or []},
        )
        return result
    except (RepositoryError, ValueError) as exc:
        _record_m12_event(
            "DEPLOYMENT_BLOCKED",
            source_module="trt_core.api.deployment.simulated_deploy",
            run_id=str(payload["run_id"]),
            scenario_spec_id=str(payload["scenario_spec_id"]),
            trt_id=str(payload["trt_id"]),
            trt_version=str(payload["trt_version"]),
            operator_id=payload.get("operator_id"),
            payload={"error": str(exc)},
        )
        return {
            "status": "FAILED",
            "deployment_id": None,
            "trt_id": payload.get("trt_id"),
            "trt_version": payload.get("trt_version"),
            "message": str(exc),
            "errors": [str(exc)],
        }


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

