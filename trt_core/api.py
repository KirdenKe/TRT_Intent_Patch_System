"""Minimal FastAPI integration surface for n8n orchestration."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from scenario_generation.errors import OperatorResolutionRequiredError, ScenarioGenerationError
from scenario_generation.generator import generate_scenario_spec
from scenario_generation.template_registry import load_template_registry
from trt_core.errors import RepositoryError
from trt_core.intent_normalizer import DOMAIN_CANDIDATE_SCHEMA, LLM_EXTRACTED_FIELDS_SCHEMA, normalize_domain_candidate
from trt_core.patch_apply import apply_intent_patch, validate_intent_patch
from trt_core.release import prepare_release, record_release_decision
from trt_core.reconciliation import load_plan
from trt_core.repository import TRTRepository
from trt_core.state_records import load_current_state, save_current_state
from trt_core.supervisor import reconcile_current_trt
from trt_core.validator import SUPPORTED_OPERATIONS


app = FastAPI(title="TRT Intent Patch Core", version="0.1.0")
repository = TRTRepository()


def _scenario_template_registry_path() -> Any:
    return repository.root / "data" / "scenario_templates.json"


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
    return {
        "current_trt": current_trt,
        "allowed_patch_operation_types": sorted(SUPPORTED_OPERATIONS),
        "editable_path_whitelist": [
            "/lines/{line_id}/goal",
            "/lines/{line_id}/allowed_instruments",
            "/lines/{line_id}/allowed_instruments/{index}",
            "/lines/{line_id}/excluded_instruments",
            "/lines/{line_id}/excluded_instruments/{index}",
            "/lines/{line_id}/priority",
            "/lines/{line_id}/kpi/deadline_minutes",
            "/lines/{line_id}/kpi/max_downtime_seconds",
            "/lines/{line_id}/kpi/min_throughput_per_hour",
            "/lines/{line_id}/abnormal_strategy",
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
            "instrument_type": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
            "abnormal_strategy": ["STOP_LINE", "CONTINUE_FEASIBLE_TASKS", "ASK_OPERATOR"],
            "line_mode": ["IDLE", "RUNNING", "INTERVENTION", "PAUSED", "ERROR"],
        },
        "llm_candidate_generation_schema": LLM_EXTRACTED_FIELDS_SCHEMA,
        "domain_candidate_internal_schema": DOMAIN_CANDIDATE_SCHEMA,
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


@app.get("/audit/{audit_id}")
def get_audit(audit_id: str) -> dict[str, Any]:
    try:
        return repository.load_audit_bundle(audit_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/intent/context")
def get_intent_context(trt_id: str | None = None) -> dict[str, Any]:
    try:
        return build_intent_context(repository.get_current_trt(trt_id))
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/intent/normalize")
def post_intent_normalize(candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        current_trt = repository.get_current_trt(candidate.get("trt_id"))
        return {"intent_patch": normalize_domain_candidate(candidate, current_trt)}
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    try:
        state_records = body.get("state_records") or load_current_state(repository)
        return reconcile_current_trt(state_records, repository, body.get("trt_id"))
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/reconciliation/list")
def get_reconciliation_list() -> dict[str, Any]:
    return {"reconciliation_plans": repository.list_reconciliation_plans()}


@app.get("/reconciliation/{plan_id}")
def get_reconciliation(plan_id: str) -> dict[str, Any]:
    try:
        return load_plan(plan_id, repository)
    except RepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/scenario/templates")
def get_scenario_templates() -> dict[str, Any]:
    try:
        return load_template_registry(_scenario_template_registry_path())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Scenario template registry not found") from exc


@app.post("/scenario/generate")
def post_scenario_generate(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"release_id", "trt_id", "trt_version", "reconciliation_plan_id", "scenario_template_id"}
    missing = sorted(field for field in required if not payload.get(field))
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing scenario generation fields: {', '.join(missing)}")
    try:
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
        reconciliation_plan["release_id"] = payload["release_id"]
        line_decisions = reconciliation_plan.get("line_decisions") or []
        affected_lines = payload.get("affected_lines") or []
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
            scenario_template_id=payload["scenario_template_id"],
            candidate_strategy_id=payload.get("candidate_strategy_id") or f"strategy_{payload['reconciliation_plan_id']}",
            output_path=repository.root / "outputs",
            template_registry=load_template_registry(_scenario_template_registry_path()),
            include_waiting_scenarios=bool(payload.get("include_waiting_scenarios", False)),
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
    except ScenarioGenerationError as exc:
        return {"status": "REJECTED", "rejection_reason": str(exc)}

    if result.get("status") == "WAITING_FOR_CHECKPOINT":
        return result
    return {
        "status": "GENERATED",
        "scenario_spec_id": result["scenario_spec_id"],
        "scenario_spec_path": result["workspace_contract"]["expected_scenario_spec_path"],
        "scenario_spec": result,
    }
