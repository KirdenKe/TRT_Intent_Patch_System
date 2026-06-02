from __future__ import annotations

import json
from copy import deepcopy

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

import trt_core.api as api
from trt_core.intent_normalizer import LLM_EXTRACTED_FIELDS_SCHEMA, validate_domain_candidate
from trt_core.repository import TRTRepository
from trt_core.validator import validate_release_record_schema


def make_client(tmp_path, fixture_loader) -> TestClient:
    repository = TRTRepository(tmp_path)
    repository.save_trt(fixture_loader("trt_v1.json"))
    api.repository = repository
    return TestClient(api.app)


def make_scenario_client(tmp_path, fixture_loader) -> TestClient:
    repository = TRTRepository(tmp_path)
    released_v1 = fixture_loader("released_trt_v1.json")
    released_v2 = deepcopy(released_v1)
    released_v2["version"] = "v2"
    repository.save_trt(released_v1)
    repository.save_trt(released_v2)
    repository.save_state_records(fixture_loader("state_records_v1.json"))
    repository.save_reconciliation_plan(fixture_loader("reconciliation_ready.json"))
    registry = deepcopy(fixture_loader("scenario_templates.json"))
    registry["default_template_id"] = "surgical_sorting_v1"
    registry["templates"][0]["template_id"] = "surgical_sorting_v1"
    registry_path = tmp_path / "data" / "scenario_templates.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    api.repository = repository
    return TestClient(api.app)


def test_get_current_trt_returns_current_trt(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.get("/trt/current")

    assert response.status_code == 200
    assert response.json()["trt_id"] == "trt-demo"
    assert response.json()["version"] == "v1"


def test_health_endpoint_returns_ok(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_scenario_generate_route_is_registered_in_openapi():
    assert "/scenario/generate" in api.app.openapi()["paths"]


def test_trt_versions_lists_stored_versions(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)

    response = client.get("/trt/versions")
    body = response.json()

    assert response.status_code == 200
    assert body == {"all_available_trts": [{"trt_id": "trt-demo", "versions": ["v1", "v2"]}]}


def test_trt_versions_for_requested_trt_id_includes_grouped_all_available(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)

    response = client.get("/trt/versions", params={"trt_id": "trt-missing"})
    body = response.json()

    assert response.status_code == 200
    assert body == {
        "available_for_requested_trt_id": [],
        "all_available_trts": [{"trt_id": "trt-demo", "versions": ["v1", "v2"]}],
    }


def test_discovery_endpoints_for_release_reconciliation_and_scenario_templates(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)

    release_response = client.get("/release/list")
    reconciliation_response = client.get("/reconciliation/list")
    templates_response = client.get("/scenario/templates")

    assert release_response.status_code == 200
    assert release_response.json() == {"releases": []}
    assert reconciliation_response.status_code == 200
    assert reconciliation_response.json()["reconciliation_plans"][0]["plan_id"] == "rec_ready_001"
    assert templates_response.status_code == 200
    assert templates_response.json()["default_template_id"] == "surgical_sorting_v1"


def test_scenario_generate_missing_trt_version_returns_available_versions(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)

    response = client.post(
        "/scenario/generate",
        json={
            "release_id": "rel_api_scenario_001",
            "trt_id": "trt-demo",
            "trt_version": "v9",
            "reconciliation_plan_id": "rec_ready_001",
            "scenario_template_id": "surgical_sorting_v1",
        },
    )
    body = response.json()

    assert response.status_code == 404
    assert body == {
        "detail": "TRT version not found",
        "requested": {"trt_id": "trt-demo", "trt_version": "v9"},
        "available_for_requested_trt_id": ["v1", "v2"],
        "all_available_trts": [{"trt_id": "trt-demo", "versions": ["v1", "v2"]}],
    }


def test_scenario_generate_missing_required_fields_returns_400(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)

    response = client.post(
        "/scenario/generate",
        json={
            "release_id": "rel_api_scenario_001",
            "trt_id": "trt-demo",
            "trt_version": None,
            "reconciliation_plan_id": "",
            "scenario_template_id": "surgical_sorting_v1",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Missing scenario generation fields: reconciliation_plan_id, trt_version"
    }


def test_scenario_generate_no_change_plan_without_affected_lines_returns_409(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)
    no_change_plan = deepcopy(fixture_loader("reconciliation_ready.json"))
    no_change_plan["plan_id"] = "rec_no_change_001"
    for decision in no_change_plan["line_decisions"]:
        decision["decision"] = "NO_CHANGE"
        decision["reason"] = "unchanged"
        decision["next_action"] = "continue"
    no_change_plan["overall_status"] = "READY"
    api.repository.save_reconciliation_plan(no_change_plan)

    response = client.post(
        "/scenario/generate",
        json={
            "release_id": "rel_api_scenario_001",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "reconciliation_plan_id": "rec_no_change_001",
            "scenario_template_id": "surgical_sorting_v1",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Reconciliation plan contains no changed lines."}


def test_scenario_generate_no_change_plan_with_affected_lines_can_generate(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)
    no_change_plan = deepcopy(fixture_loader("reconciliation_ready.json"))
    no_change_plan["plan_id"] = "rec_no_change_impact_001"
    for decision in no_change_plan["line_decisions"]:
        decision["decision"] = "NO_CHANGE"
        decision["reason"] = "unchanged"
        decision["next_action"] = "continue"
    no_change_plan["overall_status"] = "READY"
    api.repository.save_reconciliation_plan(no_change_plan)

    response = client.post(
        "/scenario/generate",
        json={
            "release_id": "rel_api_scenario_001",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "reconciliation_plan_id": "rec_no_change_impact_001",
            "scenario_template_id": "surgical_sorting_v1",
            "affected_lines": ["line_1"],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "GENERATED"


def test_scenario_generate_request_no_change_decisions_without_affected_lines_returns_409(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)
    line_decisions = deepcopy(fixture_loader("reconciliation_ready.json")["line_decisions"])
    for decision in line_decisions:
        decision["decision"] = "NO_CHANGE"
        decision["reason"] = "unchanged"
        decision["next_action"] = "continue"

    response = client.post(
        "/scenario/generate",
        json={
            "release_id": "rel_api_scenario_001",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "reconciliation_plan_id": "rec_ready_001",
            "scenario_template_id": "surgical_sorting_v1",
            "affected_lines": [],
            "line_decisions": line_decisions,
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Reconciliation plan contains no changed lines."}


def test_scenario_generate_request_no_change_decisions_with_affected_lines_can_generate(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)
    line_decisions = deepcopy(fixture_loader("reconciliation_ready.json")["line_decisions"])
    for decision in line_decisions:
        decision["decision"] = "NO_CHANGE"
        decision["reason"] = "unchanged"
        decision["next_action"] = "continue"

    response = client.post(
        "/scenario/generate",
        json={
            "release_id": "rel_api_scenario_001",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "reconciliation_plan_id": "rec_ready_001",
            "scenario_template_id": "surgical_sorting_v1",
            "affected_lines": ["line_1"],
            "line_decisions": line_decisions,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "GENERATED"


def test_post_scenario_generate_creates_scenario_spec(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)
    stored_version = client.get("/trt/versions", params={"trt_id": "trt-demo"}).json()[
        "available_for_requested_trt_id"
    ][0]

    response = client.post(
        "/scenario/generate",
        json={
            "release_id": "rel_api_scenario_001",
            "trt_id": "trt-demo",
            "trt_version": stored_version,
            "reconciliation_plan_id": "rec_ready_001",
            "scenario_template_id": "surgical_sorting_v1",
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "GENERATED"
    assert body["scenario_spec_id"].startswith("scn_")
    assert body["scenario_spec_path"] == f"outputs/scenario_specs/scenario_spec_{body['scenario_spec_id']}.json"
    assert (tmp_path / body["scenario_spec_path"]).exists()
    assert not (tmp_path / "exchange" / "scenario_specs").exists()
    assert body["scenario_spec"]["release_id"] == "rel_api_scenario_001"
    assert body["scenario_spec"]["workspace_contract"]["exchange_mode"] == "file"
    assert "outputs/scenario_specs" in body["scenario_spec"]["workspace_contract"]["expected_scenario_spec_path"]
    assert "outputs/run_artifacts" in body["scenario_spec"]["workspace_contract"]["expected_run_artifact_path"]


def test_post_scenario_generate_returns_clear_export_error(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    (output_root / "scenario_specs").write_text("not a directory", encoding="utf-8")

    response = client.post(
        "/scenario/generate",
        json={
            "release_id": "rel_api_scenario_001",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "reconciliation_plan_id": "rec_ready_001",
            "scenario_template_id": "surgical_sorting_v1",
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"].startswith("ScenarioSpec output parent exists but is not a directory:")
    assert "FileExistsError" not in response.text


def test_scenario_generate_ask_operator_requires_operator_resolution(tmp_path, fixture_loader):
    client = make_scenario_client(tmp_path, fixture_loader)
    trt = api.repository.load_trt("trt-demo", "v1")
    trt["lines"]["line_1"]["abnormal_strategy"] = "ASK_OPERATOR"
    api.repository.save_trt(trt)

    response = client.post(
        "/scenario/generate",
        json={
            "release_id": "rel_api_scenario_001",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "reconciliation_plan_id": "rec_ready_001",
            "scenario_template_id": "surgical_sorting_v1",
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body == {
        "status": "REQUIRES_OPERATOR_RESOLUTION",
        "rejection_reason": (
            "Line line_1 abnormal_strategy is ASK_OPERATOR. "
            "ScenarioSpec generation requires a concrete executable policy. "
            "Resolve this field to STOP_LINE or CONTINUE_FEASIBLE_TASKS before simulation."
        ),
        "line_id": "line_1",
        "field": "/lines/line_1/abnormal_strategy",
        "current_value": "ASK_OPERATOR",
        "allowed_values": ["CONTINUE_FEASIBLE_TASKS", "STOP_LINE"],
    }


def test_patch_validate_accepts_valid_patch_without_new_version(tmp_path, fixture_loader, valid_patch):
    client = make_client(tmp_path, fixture_loader)

    response = client.post("/patch/validate", json=valid_patch)

    assert response.status_code == 200
    assert response.json()["status"] == "ACCEPTED"
    assert api.repository.get_current_trt("trt-demo")["version"] == "v1"
    assert len(api.repository.list_trt_versions("trt-demo")) == 1
    assert list(api.repository.audit_dir.glob("*.json")) == []


def test_patch_apply_accepts_valid_patch_and_creates_version_plus_audit(tmp_path, fixture_loader, valid_patch):
    client = make_client(tmp_path, fixture_loader)

    response = client.post("/patch/apply", json=valid_patch)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ACCEPTED"
    assert body["trt_version"] == "v2"
    assert api.repository.get_current_trt("trt-demo")["version"] == "v2"
    assert api.repository.load_audit_bundle(body["audit_id"])["status"] == "ACCEPTED"


def test_patch_apply_rejects_semantic_patch_and_creates_audit(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.post("/patch/apply", json=fixture_loader("invalid_patch_semantic_conflict.json"))
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "REJECTED"
    assert body["validation_results"]["semantic"] is False
    assert api.repository.get_current_trt("trt-demo")["version"] == "v1"
    assert api.repository.load_audit_bundle(body["audit_id"])["status"] == "REJECTED"


def test_patch_apply_rejects_stale_base_version(tmp_path, fixture_loader, valid_patch):
    client = make_client(tmp_path, fixture_loader)

    accepted = client.post("/patch/apply", json=valid_patch)
    stale_patch = deepcopy(valid_patch)
    stale_patch["patch_id"] = "patch-api-stale-001"
    rejected = client.post("/patch/apply", json=stale_patch)

    assert accepted.status_code == 200
    assert accepted.json()["status"] == "ACCEPTED"
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["validation_results"]["base_version"] is False
    assert api.repository.load_audit_bundle(rejected.json()["audit_id"])["status"] == "REJECTED"


def test_get_intent_context_returns_llm_generation_context(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.get("/intent/context")
    body = response.json()

    assert response.status_code == 200
    assert body["current_trt"]["trt_id"] == "trt-demo"
    assert body["current_trt"]["version"] == "v1"
    assert body["allowed_patch_operation_types"] == ["add", "remove", "replace", "test"]
    assert "/lines/{line_id}/priority" in body["editable_path_whitelist"]
    assert "/lines/{line_id}/state/mode" in body["read_only_paths"]
    assert body["enum_values"]["goal"] == ["ROUTINE_CLASSIFICATION", "TRAUMA_SET_PRIORITY", "BACKLOG_CLEARING"]
    assert body["enum_values"]["instrument_type"] == ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"]
    assert body["enum_values"]["abnormal_strategy"] == ["STOP_LINE", "CONTINUE_FEASIBLE_TASKS", "ASK_OPERATOR"]
    assert body["enum_values"]["line_mode"] == ["IDLE", "RUNNING", "INTERVENTION", "PAUSED", "ERROR"]
    assert body["llm_candidate_generation_schema"]["required"] == [
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
    ]
    assert set(body["llm_candidate_generation_schema"]["properties"]) == {
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
    }
    assert body["llm_candidate_generation_schema"]["properties"]["action"]["enum"] == [
        "PROPOSE_PATCH",
        "NEEDS_CLARIFICATION",
        "UNSUPPORTED_REQUEST",
    ]
    assert body["llm_candidate_generation_schema"]["properties"]["line_id"]["enum"] == [
        "line_1",
        "line_2",
        "line_3",
        "line_4",
        None,
    ]
    assert body["llm_candidate_generation_schema"]["properties"]["target_scope"]["enum"] == [
        "SINGLE_LINE",
        "MULTIPLE_LINES",
        "ALL_LINES",
        None,
    ]
    assert "KPI_LIMIT_UPDATE" in body["llm_candidate_generation_schema"]["properties"]["detected_request_types"]["items"]["enum"]
    assert "PRIORITY_UPDATE" in body["llm_candidate_generation_schema"]["properties"]["detected_request_types"]["items"]["enum"]
    assert "TOOLING_POLICY_UPDATE" in body["llm_candidate_generation_schema"]["properties"]["detected_request_types"]["items"]["enum"]
    assert "MULTI_LINE_POLICY_UPDATE" in body["llm_candidate_generation_schema"]["properties"]["detected_request_types"]["items"]["enum"]
    assert body["llm_candidate_generation_schema"]["properties"]["request_types"]["items"]["enum"] == body[
        "llm_candidate_generation_schema"
    ]["properties"]["detected_request_types"]["items"]["enum"]
    assert "operations" not in body["llm_candidate_generation_schema"]["properties"]
    assert body["domain_candidate_internal_schema"]["required"] == [
        "patch_id",
        "trt_id",
        "base_version",
        "operator_id",
        "intent_text",
        "reason",
        "excluded_instruments",
        "status",
    ]
    assert body["intent_patch_internal_schema"]["required"] == [
        "patch_id",
        "trt_id",
        "base_version",
        "operator_id",
        "intent_text",
        "reason",
        "operations",
        "status",
    ]
    assert len(body["few_shot_examples"]) == 3
    assert body["few_shot_examples"][0]["name"] == "valid trauma set priority patch"
    assert body["few_shot_examples"][1]["name"] == "valid exclude instrument type patch"
    assert body["few_shot_examples"][2]["name"] == "invalid semantic conflict example"
    assert "Invalid because" in body["few_shot_examples"][2]["explanation"]


def test_debug_intent_schema_shows_active_v2_fields(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.get("/debug/intent-schema")
    body = response.json()

    assert response.status_code == 200
    for field in (
        "action",
        "line_id",
        "goal",
        "priority",
        "allowed_instruments",
        "excluded_instruments",
        "clarification_questions",
        "unsupported_terms",
        "detected_request_types",
        "target_scope",
        "target_lines",
        "request_types",
        "abnormal_strategy",
        "kpi_updates",
        "tooling_policy",
    ):
        assert field in body["domain_candidate_fields"]
    assert "request_types" in body["llm_candidate_generation_fields"]


def test_debug_intent_normalizer_runtime_reports_route_schema(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.get("/debug/intent-normalizer-runtime")
    body = response.json()

    assert response.status_code == 200
    assert body["route_model_or_schema_used_by_normalize_endpoint"].startswith("FastAPI receives candidate as dict")
    for field in (
        "target_scope",
        "target_lines",
        "request_types",
        "abnormal_strategy",
        "allowed_instruments",
        "priority",
        "kpi_updates",
        "tooling_policy",
    ):
        assert field in body["domain_candidate_fields"]
    assert "request_types" in body["llm_extracted_fields"]


def test_intent_context_logs_tooling_policy_schema(tmp_path, fixture_loader, caplog):
    client = make_client(tmp_path, fixture_loader)

    caplog.set_level("INFO")
    response = client.get("/intent/context")
    logs = "\n".join(record.message for record in caplog.records)

    assert response.status_code == 200
    assert "intent_context.llm_candidate_generation_schema.properties.tooling_policy=" in logs
    assert "'required': ['required_scope']" in logs
    assert "all_required" not in response.text


def test_llm_domain_schema_requires_tooling_policy_required_scope_and_forbids_all_required():
    candidate = {
        "action": "PROPOSE_PATCH",
        "line_id": None,
        "target_scope": "ALL_LINES",
        "target_lines": [],
        "goal": None,
        "priority": None,
        "excluded_instruments": None,
        "allowed_instruments": None,
        "kpi_updates": {"deadline_minutes": None, "max_downtime_seconds": None},
        "tooling_policy": {"all_required": True},
        "abnormal_strategy": None,
        "clarification_questions": [],
        "unsupported_terms": [],
        "detected_request_types": ["MULTI_LINE_POLICY_UPDATE", "TOOLING_POLICY_UPDATE", "KPI_LIMIT_UPDATE"],
        "request_types": ["MULTI_LINE_POLICY_UPDATE", "TOOLING_POLICY_UPDATE", "KPI_LIMIT_UPDATE"],
    }

    errors = [error.message for error in Draft202012Validator(LLM_EXTRACTED_FIELDS_SCHEMA).iter_errors(candidate)]

    assert "Additional properties are not allowed ('all_required' was unexpected)" in errors
    assert "'required_scope' is a required property" in errors


def test_debug_reset_demo_trt_state_requires_dev_or_test_env(tmp_path, fixture_loader, monkeypatch):
    client = make_client(tmp_path, fixture_loader)
    monkeypatch.delenv("APP_ENV", raising=False)

    response = client.post("/debug/reset-demo-trt-state")

    assert response.status_code == 403
    assert response.json()["detail"] == "Debug reset is only available when APP_ENV is dev or test."


def test_debug_reset_demo_trt_state_resets_only_line_2_state(tmp_path, fixture_loader, monkeypatch):
    client = make_client(tmp_path, fixture_loader)
    monkeypatch.setenv("APP_ENV", "test")
    before = deepcopy(api.repository.get_current_trt("trt-demo")["lines"]["line_2"])

    response = client.post("/debug/reset-demo-trt-state")
    after = api.repository.get_current_trt("trt-demo")["lines"]["line_2"]

    assert response.status_code == 200
    assert response.json() == {
        "trt_id": "trt-demo",
        "version": "v1",
        "line_id": "line_2",
        "state": {
            "mode": "RUNNING",
            "last_exception": None,
            "current_task": None,
            "wip_count": 0,
        },
    }
    assert after["state"] == response.json()["state"]
    for field in ("goal", "kpi", "allowed_instruments", "excluded_instruments", "abnormal_strategy", "tooling_policy"):
        assert after.get(field) == before.get(field)


def test_debug_reset_demo_runtime_state_resets_supervisor_state_source(tmp_path, fixture_loader, monkeypatch):
    repository = TRTRepository(tmp_path)
    base_trt = fixture_loader("trt_v1.json")
    changed_trt = deepcopy(base_trt)
    changed_trt["version"] = "v2"
    changed_trt["lines"]["line_2"]["priority"] = 5
    repository.save_trt(base_trt)
    repository.save_trt(changed_trt)
    repository.save_state_records(
        [
            {
                "line_id": "line_1",
                "mode": "ERROR",
                "last_exception": "jam_detected",
                "current_task": "TRAUMA_SET_PRIORITY",
                "wip_count": 4,
                "current_instruments": ["SCISSORS"],
                "checkpoint": "MANUAL_CLEARANCE_REQUIRED",
                "locked_resources": ["robot_arm_1"],
            },
            {
                "line_id": "line_2",
                "mode": "ERROR",
                "last_exception": "jam_detected",
                "current_task": None,
                "wip_count": 0,
                "current_instruments": [],
                "checkpoint": "MANUAL_CLEARANCE_REQUIRED",
                "locked_resources": ["robot_arm_2"],
            },
        ]
    )
    api.repository = repository
    client = TestClient(api.app)
    monkeypatch.setenv("APP_ENV", "test")

    reset_response = client.post("/debug/reset-demo-runtime-state")
    state_response = client.get("/debug/supervisor-state")
    reconcile_response = client.post("/supervisor/reconcile", json={"trt_id": "trt-demo"})

    assert reset_response.status_code == 200
    assert reset_response.json()["state_source"] == "data/state_records/current_state.json"
    assert state_response.json()["state_records"] == reset_response.json()["state_records"]
    for record in state_response.json()["state_records"]:
        assert record["mode"] == "RUNNING"
        assert record["last_exception"] is None
        assert record["current_task"] is None
        assert record["wip_count"] == 0

    assert reconcile_response.status_code == 200
    line_2 = next(
        decision for decision in reconcile_response.json()["line_decisions"] if decision["line_id"] == "line_2"
    )
    assert line_2["decision"] != "REJECT_INCOMPATIBLE"
    assert "line_error" not in line_2["risk_flags"]


def test_debug_migrate_demo_trt_tooling_policy_requires_dev_or_test_env(tmp_path, fixture_loader, monkeypatch):
    client = make_client(tmp_path, fixture_loader)
    monkeypatch.delenv("APP_ENV", raising=False)

    response = client.post("/debug/migrate-demo-trt-tooling-policy")

    assert response.status_code == 403
    assert response.json()["detail"] == "Debug migration is only available when APP_ENV is dev or test."


def test_debug_migrate_demo_trt_tooling_policy_rewrites_legacy_fields(tmp_path, fixture_loader, monkeypatch):
    trt = fixture_loader("trt_v1.json")
    trt["lines"]["line_1"]["tooling_policy"] = {"all_required": True}
    trt["lines"]["line_2"]["tooling_policy"] = {"all_required": False}
    repository = TRTRepository(tmp_path)
    repository.save_trt(trt)
    api.repository = repository
    client = TestClient(api.app)
    monkeypatch.setenv("APP_ENV", "test")

    response = client.post("/debug/migrate-demo-trt-tooling-policy")
    current = api.repository.get_current_trt("trt-demo")

    assert response.status_code == 200
    assert response.json()["migrated_versions"] == [{"trt_id": "trt-demo", "version": "v1"}]
    assert current["lines"]["line_1"]["tooling_policy"] == {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}
    assert current["lines"]["line_2"]["tooling_policy"] == {"required_scope": "NONE"}


def test_get_intent_context_does_not_create_versions_or_audits(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.get("/intent/context")

    assert response.status_code == 200
    assert len(api.repository.list_trt_versions("trt-demo")) == 1
    assert list(api.repository.audit_dir.glob("*.json")) == []


def domain_candidate() -> dict:
    return {
        "patch_id": "domain-candidate-001",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "Make Line 1 prioritize Trauma Set and exclude forceps",
        "reason": "urgent trauma set deadline",
        "line_id": "line_1",
        "goal": "TRAUMA_SET_PRIORITY",
        "excluded_instruments": ["FORCEPS"],
        "status": "REVIEWED",
    }


def test_intent_normalize_converts_domain_candidate_to_patch_operations(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.post("/intent/normalize", json=domain_candidate())
    intent_patch = response.json()["intent_patch"]

    assert response.status_code == 200
    assert intent_patch["operations"] == [
        {"op": "replace", "path": "/lines/line_1/goal", "value": "TRAUMA_SET_PRIORITY"},
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": ["FORCEPS"]},
    ]


def test_intent_normalize_clears_excluded_instruments_when_empty_list_is_explicit(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "goal": None,
        "excluded_instruments": [],
        "request_types": ["INSTRUMENT_SCOPE_UPDATE"],
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": []}
    ]


def test_intent_normalize_entanglement_strategy_does_not_create_excluded_instruments(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "If entanglement occurs, continue feasible tasks when safe.",
        "goal": None,
        "excluded_instruments": None,
        "abnormal_strategy": "CONTINUE_FEASIBLE_TASKS",
        "request_types": ["ABNORMAL_STRATEGY_UPDATE"],
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {"op": "replace", "path": "/lines/line_1/abnormal_strategy", "value": "CONTINUE_FEASIBLE_TASKS"}
    ]


def test_intent_normalize_exclude_forceps_still_sets_excluded_instruments(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "goal": None,
        "excluded_instruments": ["FORCEPS"],
        "request_types": ["INSTRUMENT_SCOPE_UPDATE"],
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": ["FORCEPS"]}
    ]


def test_patch_validate_accepts_empty_allowed_instruments_as_no_tooling_selected(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    intent_patch = {
        "patch_id": "patch-empty-allowed-001",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "Select no tooling for line 1.",
        "reason": "strategy requires no tooling",
        "operations": [
            {"op": "replace", "path": "/lines/line_1/allowed_instruments", "value": []},
            {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": []},
        ],
        "status": "REVIEWED",
    }

    response = client.post("/patch/validate", json=intent_patch)

    assert response.status_code == 200
    assert response.json()["status"] == "ACCEPTED"
    assert response.json()["validation_results"]["semantic"] is True
    assert "allowed_instruments must not be empty" not in response.text


def test_intent_normalize_no_tooling_selected_accepts_empty_allowed_instruments(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Line 1 select no tooling for this strategy.",
        "goal": None,
        "allowed_instruments": [],
        "excluded_instruments": [],
        "request_types": ["TOOLING_POLICY_UPDATE", "INSTRUMENT_SCOPE_UPDATE"],
        "tooling_policy": {"required_scope": "NONE"},
    }

    normalize_response = client.post("/intent/normalize", json=candidate)
    validate_response = client.post("/patch/validate", json=normalize_response.json()["intent_patch"])

    assert normalize_response.status_code == 200
    assert normalize_response.json()["intent_patch"]["operations"] == [
        {"op": "add", "path": "/lines/line_1/tooling_policy", "value": {"required_scope": "NONE"}},
        {"op": "replace", "path": "/lines/line_1/allowed_instruments", "value": []},
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": []},
    ]
    assert validate_response.status_code == 200
    assert validate_response.json()["status"] == "ACCEPTED"


def test_intent_normalize_empty_allowed_instruments_does_not_force_exclusion_complement(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Line 1 select no tooling for this strategy.",
        "goal": None,
        "allowed_instruments": [],
        "excluded_instruments": None,
        "request_types": ["INSTRUMENT_SCOPE_UPDATE"],
        "tooling_policy": {"required_scope": "NONE"},
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    operations = response.json()["intent_patch"]["operations"]
    assert {"op": "replace", "path": "/lines/line_1/allowed_instruments", "value": []} in operations
    assert not any(
        operation["path"] == "/lines/line_1/excluded_instruments"
        and operation["value"] == ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"]
        for operation in operations
    )


def test_patch_validate_empty_allowed_and_empty_excluded_are_independent_valid_states(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    intent_patch = {
        "patch_id": "patch-empty-selected-and-excluded-001",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "Select no tooling and no explicit exclusions for line 1.",
        "reason": "strategy uses no tooling",
        "operations": [
            {"op": "replace", "path": "/lines/line_1/allowed_instruments", "value": []},
            {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": []},
            {"op": "add", "path": "/lines/line_1/tooling_policy", "value": {"required_scope": "NONE"}},
        ],
        "status": "REVIEWED",
    }

    response = client.post("/patch/validate", json=intent_patch)

    assert response.status_code == 200
    assert response.json()["status"] == "ACCEPTED"
    assert response.json()["validation_results"]["semantic"] is True
    assert "allowed_instruments must not be empty" not in response.text
    assert "excluded_instruments" not in response.text


def test_intent_normalize_do_not_want_all_tooling_selected_may_clear_allowed_instruments(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Line 1: I do not want all tooling selected.",
        "goal": None,
        "allowed_instruments": [],
        "excluded_instruments": [],
        "request_types": ["TOOLING_POLICY_UPDATE", "INSTRUMENT_SCOPE_UPDATE"],
        "tooling_policy": {"required_scope": "NONE"},
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert {"op": "replace", "path": "/lines/line_1/allowed_instruments", "value": []} in response.json()[
        "intent_patch"
    ]["operations"]


def test_intent_normalize_select_all_tooling_sets_all_supported_instruments(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Line 1 select all tooling for this strategy.",
        "goal": None,
        "allowed_instruments": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        "excluded_instruments": [],
        "request_types": ["INSTRUMENT_SCOPE_UPDATE"],
        "tooling_policy": None,
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {
            "op": "replace",
            "path": "/lines/line_1/allowed_instruments",
            "value": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        },
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": []},
    ]


def test_intent_normalize_highest_priority_sets_priority_without_goal_change(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Line 1 set priority to the highest level.",
        "goal": None,
        "priority": 5,
        "excluded_instruments": None,
        "request_types": ["PRIORITY_UPDATE"],
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {"op": "replace", "path": "/lines/line_1/priority", "value": 5}
    ]


def test_intent_normalize_priority_does_not_change_goal(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Line 1 should have highest priority.",
        "goal": None,
        "priority": 5,
        "excluded_instruments": None,
        "request_types": ["PRIORITY_UPDATE"],
    }

    response = client.post("/intent/normalize", json=candidate)
    operations = response.json()["intent_patch"]["operations"]

    assert response.status_code == 200
    assert {"op": "replace", "path": "/lines/line_1/priority", "value": 5} in operations
    assert not any(operation["path"] == "/lines/line_1/goal" for operation in operations)


def test_intent_normalize_rejects_priority_outside_range(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "goal": None,
        "priority": 6,
        "excluded_instruments": None,
        "request_types": ["PRIORITY_UPDATE"],
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 400
    assert "6 is greater than the maximum of 5" in response.json()["detail"]


def test_intent_normalize_trauma_set_priority_still_changes_goal(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Line 1 should prioritize Trauma Set.",
        "goal": "TRAUMA_SET_PRIORITY",
        "priority": None,
        "excluded_instruments": None,
        "request_types": ["TASK_GOAL_UPDATE"],
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {"op": "replace", "path": "/lines/line_1/goal", "value": "TRAUMA_SET_PRIORITY"}
    ]


def test_intent_normalize_accepts_domain_candidate_v2_fields_for_old_trauma_request(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "action": "PROPOSE_PATCH",
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_1"],
        "request_types": ["TASK_GOAL_UPDATE", "INSTRUMENT_SCOPE_UPDATE", "ABNORMAL_STRATEGY_UPDATE"],
        "abnormal_strategy": "CONTINUE_FEASIBLE_TASKS",
        "kpi_updates": None,
        "tooling_policy": None,
        "clarification_questions": [],
        "unsupported_terms": [],
        "detected_request_types": ["TASK_GOAL_UPDATE", "INSTRUMENT_SCOPE_UPDATE", "ABNORMAL_STRATEGY_UPDATE"],
    }

    response = client.post("/intent/normalize", json=candidate)
    intent_patch = response.json()["intent_patch"]

    assert response.status_code == 200
    assert intent_patch["operations"] == [
        {"op": "replace", "path": "/lines/line_1/goal", "value": "TRAUMA_SET_PRIORITY"},
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": ["FORCEPS"]},
        {"op": "replace", "path": "/lines/line_1/abnormal_strategy", "value": "CONTINUE_FEASIBLE_TASKS"},
    ]


def test_intent_normalize_route_accepts_all_domain_candidate_v2_fields(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "action": "PROPOSE_PATCH",
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_1"],
        "request_types": [
            "TASK_GOAL_UPDATE",
            "INSTRUMENT_SCOPE_UPDATE",
            "ABNORMAL_STRATEGY_UPDATE",
            "KPI_LIMIT_UPDATE",
            "TOOLING_POLICY_UPDATE",
        ],
        "abnormal_strategy": "CONTINUE_FEASIBLE_TASKS",
        "kpi_updates": {"deadline_minutes": None, "max_downtime_seconds": None},
        "tooling_policy": {"required_scope": "ALLOWED_INSTRUMENTS"},
        "clarification_questions": [],
        "unsupported_terms": [],
        "detected_request_types": [
            "TASK_GOAL_UPDATE",
            "INSTRUMENT_SCOPE_UPDATE",
            "ABNORMAL_STRATEGY_UPDATE",
            "KPI_LIMIT_UPDATE",
            "TOOLING_POLICY_UPDATE",
        ],
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    operations = response.json()["intent_patch"]["operations"]
    assert {"op": "replace", "path": "/lines/line_1/goal", "value": "TRAUMA_SET_PRIORITY"} in operations
    assert {
        "op": "replace",
        "path": "/lines/line_1/abnormal_strategy",
        "value": "CONTINUE_FEASIBLE_TASKS",
    } in operations
    assert {
        "op": "add",
        "path": "/lines/line_1/tooling_policy",
        "value": {"required_scope": "ALLOWED_INSTRUMENTS"},
    } in operations
    assert {"op": "replace", "path": "/lines/line_1/kpi/deadline_minutes", "value": None} in operations


def test_intent_normalize_endpoint_accepts_legacy_all_required_without_schema_error(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Require line tooling policy.",
        "goal": None,
        "excluded_instruments": None,
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_1"],
        "request_types": ["TOOLING_POLICY_UPDATE"],
        "tooling_policy": {"all_required": True},
    }

    response = client.post("/intent/normalize", json=candidate)
    body_text = response.text

    assert response.status_code == 200
    assert "Additional properties are not allowed" not in body_text
    assert "all_required" not in body_text
    assert response.json()["intent_patch"]["operations"] == [
        {"op": "add", "path": "/lines/line_1/tooling_policy", "value": {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}},
        {
            "op": "replace",
            "path": "/lines/line_1/allowed_instruments",
            "value": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        },
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": []},
    ]


def test_intent_normalize_generated_patch_never_contains_all_required(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Require line tooling policy.",
        "goal": None,
        "excluded_instruments": None,
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_1"],
        "request_types": ["TOOLING_POLICY_UPDATE"],
        "tooling_policy": {"all_required": True},
    }

    response = client.post("/intent/normalize", json=candidate)
    intent_patch = response.json()["intent_patch"]

    assert response.status_code == 200
    assert "all_required" not in json.dumps(intent_patch)
    assert intent_patch["operations"][0]["value"] == {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}


def test_intent_normalize_endpoint_accepts_required_scope_tooling_policy(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "goal": None,
        "excluded_instruments": None,
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_1"],
        "request_types": ["TOOLING_POLICY_UPDATE"],
        "tooling_policy": {"required_scope": "ALLOWED_INSTRUMENTS"},
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {"op": "add", "path": "/lines/line_1/tooling_policy", "value": {"required_scope": "ALLOWED_INSTRUMENTS"}}
    ]


def test_intent_normalize_endpoint_logs_tooling_policy_coercion(tmp_path, fixture_loader, caplog):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Require line tooling policy.",
        "goal": None,
        "excluded_instruments": None,
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_1"],
        "request_types": ["TOOLING_POLICY_UPDATE"],
        "tooling_policy": {"all_required": True},
    }

    caplog.set_level("INFO")
    response = client.post("/intent/normalize", json=candidate)
    logs = "\n".join(record.message for record in caplog.records)

    assert response.status_code == 200
    assert "raw_llm_candidate.tooling_policy={'all_required': True}" in logs
    assert "request_body_sent_to_python.tooling_policy={'all_required': True}" in logs
    assert "coerced_candidate.tooling_policy={'required_scope': 'ALL_SUPPORTED_INSTRUMENTS'}" in logs


def test_intent_normalize_backward_compat_sets_single_line_scope(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.post("/intent/normalize-domain-candidate", json=domain_candidate())

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"][0]["path"] == "/lines/line_1/goal"


def test_intent_normalize_accepts_detected_request_types_without_request_types(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_1"],
        "detected_request_types": ["TASK_GOAL_UPDATE", "INSTRUMENT_SCOPE_UPDATE"],
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {"op": "replace", "path": "/lines/line_1/goal", "value": "TRAUMA_SET_PRIORITY"},
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": ["FORCEPS"]},
    ]


def test_intent_normalize_maps_line_tooling_phrase_to_all_supported_tooling_scope(tmp_path, fixture_loader):
    trt = fixture_loader("trt_v1.json")
    trt["lines"]["line_2"]["state"]["mode"] = "IDLE"
    repository = TRTRepository(tmp_path)
    repository.save_trt(trt)
    api.repository = repository
    client = TestClient(api.app)
    candidate = {
        "patch_id": "domain-candidate-all-lines-policy-001",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": (
            "pls set all tooling required by each production line to required, "
            "with no deadline and no maximum downtime limit for each production line."
        ),
        "reason": "operator policy update",
        "line_id": None,
        "target_scope": "ALL_LINES",
        "target_lines": [],
        "request_types": [
            "MULTI_LINE_POLICY_UPDATE",
            "TOOLING_POLICY_UPDATE",
            "INSTRUMENT_SCOPE_UPDATE",
            "KPI_LIMIT_UPDATE",
        ],
        "goal": None,
        "allowed_instruments": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        "excluded_instruments": [],
        "kpi_updates": {"deadline_minutes": None, "max_downtime_seconds": None},
        "tooling_policy": {"all_required": True},
        "status": "REVIEWED",
    }

    response = client.post("/intent/normalize", json=candidate)
    intent_patch = response.json()["intent_patch"]

    assert response.status_code == 200
    assert intent_patch["operations"] == [
        {"op": "add", "path": "/lines/line_1/tooling_policy", "value": {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}},
        {
            "op": "replace",
            "path": "/lines/line_1/allowed_instruments",
            "value": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        },
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": []},
        {"op": "replace", "path": "/lines/line_1/kpi/deadline_minutes", "value": None},
        {"op": "replace", "path": "/lines/line_1/kpi/max_downtime_seconds", "value": None},
        {"op": "add", "path": "/lines/line_2/tooling_policy", "value": {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}},
        {
            "op": "replace",
            "path": "/lines/line_2/allowed_instruments",
            "value": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        },
        {"op": "replace", "path": "/lines/line_2/excluded_instruments", "value": []},
        {"op": "replace", "path": "/lines/line_2/kpi/deadline_minutes", "value": None},
        {"op": "replace", "path": "/lines/line_2/kpi/max_downtime_seconds", "value": None},
    ]


def test_intent_normalize_maps_mandatory_tooling_phrase_to_all_supported_instruments(tmp_path, fixture_loader):
    trt = fixture_loader("trt_v1.json")
    trt["lines"]["line_2"]["state"]["mode"] = "IDLE"
    repository = TRTRepository(tmp_path)
    repository.save_trt(trt)
    api.repository = repository
    client = TestClient(api.app)
    candidate = {
        "patch_id": "domain-candidate-mandatory-tooling-001",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "mark all tooling required for each production line as mandatory",
        "reason": "operator policy update",
        "line_id": None,
        "target_scope": "ALL_LINES",
        "target_lines": [],
        "request_types": ["MULTI_LINE_POLICY_UPDATE", "TOOLING_POLICY_UPDATE", "INSTRUMENT_SCOPE_UPDATE"],
        "goal": None,
        "allowed_instruments": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        "excluded_instruments": [],
        "kpi_updates": None,
        "tooling_policy": {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"},
        "status": "REVIEWED",
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {"op": "add", "path": "/lines/line_1/tooling_policy", "value": {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}},
        {
            "op": "replace",
            "path": "/lines/line_1/allowed_instruments",
            "value": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        },
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": []},
        {"op": "add", "path": "/lines/line_2/tooling_policy", "value": {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}},
        {
            "op": "replace",
            "path": "/lines/line_2/allowed_instruments",
            "value": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        },
        {"op": "replace", "path": "/lines/line_2/excluded_instruments", "value": []},
    ]


def test_intent_normalize_rejects_all_supported_scope_with_empty_allowed_instruments(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "mark all tooling required for line 1 as mandatory",
        "goal": None,
        "allowed_instruments": [],
        "excluded_instruments": [],
        "request_types": ["TOOLING_POLICY_UPDATE", "INSTRUMENT_SCOPE_UPDATE"],
        "tooling_policy": {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"},
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 400
    assert (
        "tooling_policy.required_scope=ALL_SUPPORTED_INSTRUMENTS requires allowed_instruments to contain "
        "SCISSORS, FORCEPS, CLAMPS, and RETRACTOR."
    ) in response.json()["detail"]


def test_intent_normalize_coerces_legacy_all_required_true_to_all_supported_scope(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "intent_text": "Require the line tooling policy.",
        "goal": None,
        "excluded_instruments": None,
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_1"],
        "request_types": ["TOOLING_POLICY_UPDATE"],
        "tooling_policy": {"all_required": True},
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {"op": "add", "path": "/lines/line_1/tooling_policy", "value": {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"}},
        {
            "op": "replace",
            "path": "/lines/line_1/allowed_instruments",
            "value": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        },
        {"op": "replace", "path": "/lines/line_1/excluded_instruments", "value": []},
    ]


def test_intent_normalize_maps_all_supported_instruments_phrase_to_supported_scope(tmp_path, fixture_loader):
    trt = fixture_loader("trt_v1.json")
    trt["lines"]["line_2"]["state"]["mode"] = "IDLE"
    repository = TRTRepository(tmp_path)
    repository.save_trt(trt)
    api.repository = repository
    client = TestClient(api.app)
    candidate = {
        **domain_candidate(),
        "intent_text": "all supported instruments required on every line",
        "goal": None,
        "line_id": None,
        "excluded_instruments": None,
        "target_scope": "ALL_LINES",
        "target_lines": [],
        "request_types": ["MULTI_LINE_POLICY_UPDATE", "TOOLING_POLICY_UPDATE"],
        "tooling_policy": {"all_required": True},
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"][0] == {
        "op": "add",
        "path": "/lines/line_1/tooling_policy",
        "value": {"required_scope": "ALL_SUPPORTED_INSTRUMENTS"},
    }


def test_intent_normalize_replaces_existing_tooling_policy_required_scope(tmp_path, fixture_loader):
    trt = fixture_loader("trt_v1.json")
    trt["lines"]["line_1"]["tooling_policy"] = {"required_scope": "NONE"}
    repository = TRTRepository(tmp_path)
    repository.save_trt(trt)
    api.repository = repository
    client = TestClient(api.app)
    candidate = {
        **domain_candidate(),
        "goal": None,
        "excluded_instruments": None,
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_1"],
        "request_types": ["TOOLING_POLICY_UPDATE"],
        "tooling_policy": {"required_scope": "ALLOWED_INSTRUMENTS"},
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 200
    assert response.json()["intent_patch"]["operations"] == [
        {
            "op": "replace",
            "path": "/lines/line_1/tooling_policy/required_scope",
            "value": "ALLOWED_INSTRUMENTS",
        }
    ]


def test_intent_normalize_rejects_all_lines_policy_update_when_target_line_is_error(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        "patch_id": "domain-candidate-all-lines-policy-error-001",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": (
            "pls set all tooling required by each production line to required, "
            "with no deadline and no maximum downtime limit for each production line."
        ),
        "reason": "operator policy update",
        "line_id": None,
        "target_scope": "ALL_LINES",
        "target_lines": [],
        "request_types": ["MULTI_LINE_POLICY_UPDATE", "TOOLING_POLICY_UPDATE", "KPI_LIMIT_UPDATE"],
        "goal": None,
        "allowed_instruments": ["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"],
        "excluded_instruments": [],
        "kpi_updates": {"deadline_minutes": None, "max_downtime_seconds": None},
        "tooling_policy": {"all_required": True},
        "status": "REVIEWED",
    }

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "line_2 is currently in ERROR mode. "
        "Resolve the line error or confirm that ERROR lines should be excluded."
    )


def test_domain_candidate_validation_reasons_are_deduplicated(fixture_loader):
    current_trt = fixture_loader("trt_v1.json")
    candidate = {
        **domain_candidate(),
        "target_scope": "MULTIPLE_LINES",
        "target_lines": ["line_9", "line_9"],
    }

    reasons = validate_domain_candidate(candidate, current_trt)

    assert reasons.count("'line_9' is not one of ['line_1', 'line_2', 'line_3', 'line_4']") == 1
    assert reasons.count("target line not found in current TRT: line_9") == 1


def test_intent_normalize_endpoint_returns_deduplicated_validation_errors(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "target_scope": "MULTIPLE_LINES",
        "target_lines": ["line_9", "line_9"],
    }

    response = client.post("/intent/normalize", json=candidate)
    detail = response.json()["detail"]

    assert response.status_code == 400
    assert detail.count("'line_9' is not one of ['line_1', 'line_2', 'line_3', 'line_4']") == 1
    assert detail.count("target line not found in current TRT: line_9") == 1


def test_intent_normalize_accepts_kpi_null_values_and_patch_validates(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {
        **domain_candidate(),
        "goal": None,
        "excluded_instruments": None,
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_2"],
        "line_id": "line_2",
        "request_types": ["KPI_LIMIT_UPDATE"],
        "kpi_updates": {"deadline_minutes": None, "max_downtime_seconds": None},
    }

    normalize_response = client.post("/intent/normalize", json=candidate)
    validate_response = client.post("/patch/validate", json=normalize_response.json()["intent_patch"])

    assert normalize_response.status_code == 200
    assert validate_response.status_code == 200
    assert validate_response.json()["status"] == "ACCEPTED"


def test_intent_normalize_rejects_truly_unknown_domain_candidate_fields(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = {**domain_candidate(), "unexpected_policy_field": True}

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 400
    assert "unexpected_policy_field" in response.json()["detail"]


def test_intent_normalize_rejects_invalid_line_id(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = domain_candidate()
    candidate["line_id"] = "line_9"

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 400
    assert "line_9" in response.json()["detail"]


def test_intent_normalize_rejects_invalid_instrument_enum(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)
    candidate = domain_candidate()
    candidate["excluded_instruments"] = ["LASER_SCALPEL"]

    response = client.post("/intent/normalize", json=candidate)

    assert response.status_code == 400
    assert "LASER_SCALPEL" in response.json()["detail"]


def test_normalized_patch_passes_patch_validate(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    normalize_response = client.post("/intent/normalize", json=domain_candidate())
    validate_response = client.post("/patch/validate", json=normalize_response.json()["intent_patch"])

    assert normalize_response.status_code == 200
    assert validate_response.status_code == 200
    assert validate_response.json()["status"] == "ACCEPTED"


def test_patch_validate_migrates_legacy_current_trt_tooling_policy(tmp_path, fixture_loader):
    trt = fixture_loader("trt_v1.json")
    trt["lines"]["line_1"]["tooling_policy"] = {"all_required": True}
    repository = TRTRepository(tmp_path)
    repository.save_trt(trt)
    api.repository = repository
    client = TestClient(api.app)
    intent_patch = {
        "patch_id": "patch-tooling-policy-validate-001",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "Set tooling policy required scope.",
        "reason": "validate migration",
        "operations": [
            {
                "op": "replace",
                "path": "/lines/line_1/tooling_policy/required_scope",
                "value": "ALLOWED_INSTRUMENTS",
            }
        ],
        "status": "REVIEWED",
    }

    response = client.post("/patch/validate", json=intent_patch)
    body_text = response.text

    assert response.status_code == 200
    assert response.json()["status"] == "ACCEPTED"
    assert "all_required" not in body_text


def test_patch_validate_deduplicates_schema_errors(tmp_path, fixture_loader):
    trt = fixture_loader("trt_v1.json")
    trt["lines"]["line_1"]["tooling_policy"] = {"all_required": True}
    trt["lines"]["line_2"]["tooling_policy"] = {"all_required": True}
    repository = TRTRepository(tmp_path)
    repository.save_trt(trt)
    api.repository = repository
    client = TestClient(api.app)
    intent_patch = {
        "patch_id": "patch-tooling-policy-validate-002",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "Invalid patch op for duplicate schema check.",
        "reason": "validate dedupe",
        "operations": [
            {"op": "replace", "path": "/lines/line_1/tooling_policy/all_required", "value": True}
        ],
        "status": "REVIEWED",
    }

    response = client.post("/patch/validate", json=intent_patch)
    reasons = response.json()["rejection_reasons"]

    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"
    assert reasons.count("operation 0 path is not whitelisted: /lines/line_1/tooling_policy/all_required") == 1
    assert not any("schema: Additional properties are not allowed ('all_required' was unexpected)" == reason for reason in reasons)


def prepare_release(client: TestClient, valid_patch: dict) -> dict:
    response = client.post("/release/prepare", json=valid_patch)
    assert response.status_code == 200
    return response.json()


def test_prepare_release_valid_patch_creates_pending_release_without_applying(tmp_path, fixture_loader, valid_patch):
    client = make_client(tmp_path, fixture_loader)

    prepared = prepare_release(client, valid_patch)
    release = client.get(f"/release/{prepared['release_id']}").json()

    assert prepared["status"] == "PENDING_OPERATOR_DECISION"
    assert prepared["patch_id"] == valid_patch["patch_id"]
    assert prepared["current_trt_version"] == "v1"
    assert prepared["validation_results"]["semantic"] is True
    assert prepared["release_id"].startswith("rel_")
    assert release["candidate_patch"]["patch_id"] == valid_patch["patch_id"]
    assert release["release_id"] == prepared["release_id"]
    assert release["base_version"] == "v1"
    assert release["candidate_summary"]["affected_lines"] == ["line_1"]
    assert release["candidate_summary"]["affected_fields"] == [
        "/lines/line_1/goal",
        "/lines/line_1/priority",
        "/lines/line_1/kpi/deadline_minutes",
    ]
    assert release["validation_results_at_prepare"]["semantic"] is True
    assert release["operator_decision"] is None
    assert validate_release_record_schema(release) == []
    assert (api.repository.release_dir / f"{prepared['release_id']}.json").exists()
    assert api.repository.get_current_trt("trt-demo")["version"] == "v1"
    assert list(api.repository.audit_dir.glob("*.json")) == []


def test_approve_release_applies_patch_and_marks_released(tmp_path, fixture_loader, valid_patch):
    client = make_client(tmp_path, fixture_loader)
    prepared = prepare_release(client, valid_patch)

    response = client.post(
        "/release/decision",
        json={
            "release_id": prepared["release_id"],
            "operator_id": "op_001",
            "decision": "APPROVE",
            "comment": "Approved for release.",
        },
    )
    body = response.json()
    audit = api.repository.load_audit_bundle(body["audit_id"])

    assert response.status_code == 200
    assert body["status"] == "RELEASED"
    assert body["operator_decision"]["decision"] == "APPROVE"
    assert body["operator_decision"]["comment"] == "Approved for release."
    assert validate_release_record_schema(body) == []
    assert api.repository.get_current_trt("trt-demo")["version"] == "v2"
    assert audit["status"] in {"ACCEPTED", "RELEASED"}


def test_reject_release_is_auditable_without_applying(tmp_path, fixture_loader, valid_patch):
    client = make_client(tmp_path, fixture_loader)
    prepared = prepare_release(client, valid_patch)

    response = client.post(
        "/release/decision",
        json={
            "release_id": prepared["release_id"],
            "operator_id": "op_001",
            "decision": "REJECT",
            "comment": "Do not release during shift change.",
        },
    )
    body = response.json()
    audit = api.repository.load_audit_bundle(body["audit_id"])

    assert response.status_code == 200
    assert body["status"] == "REJECTED_BY_OPERATOR"
    assert body["operator_decision"]["decision"] == "REJECT"
    assert body["operator_decision"]["comment"] == "Do not release during shift change."
    assert validate_release_record_schema(body) == []
    assert api.repository.get_current_trt("trt-demo")["version"] == "v1"
    assert audit["status"] == "REJECTED_BY_OPERATOR"
    assert audit["operator_id"] == "op_001"
    assert audit["trt_after_version"] is None
    assert audit["rejection_reasons"] == ["Do not release during shift change."]


def test_request_revision_is_auditable_without_applying(tmp_path, fixture_loader, valid_patch):
    client = make_client(tmp_path, fixture_loader)
    prepared = prepare_release(client, valid_patch)

    response = client.post(
        "/release/decision",
        json={
            "release_id": prepared["release_id"],
            "operator_id": "op_001",
            "decision": "REQUEST_REVISION",
            "comment": "Add more operator rationale.",
        },
    )
    body = response.json()
    audit = api.repository.load_audit_bundle(body["audit_id"])

    assert response.status_code == 200
    assert body["status"] == "NEEDS_REVISION"
    assert body["operator_decision"]["decision"] == "REQUEST_REVISION"
    assert body["operator_decision"]["comment"] == "Add more operator rationale."
    assert validate_release_record_schema(body) == []
    assert api.repository.get_current_trt("trt-demo")["version"] == "v1"
    assert audit["status"] == "NEEDS_REVISION"
    assert audit["operator_id"] == "op_001"
    assert audit["rejection_reasons"] == ["Add more operator rationale."]


def test_stale_release_approval_rejects_stale_base_version_without_overwrite(tmp_path, fixture_loader, valid_patch):
    client = make_client(tmp_path, fixture_loader)
    prepared = prepare_release(client, valid_patch)

    other_patch = deepcopy(valid_patch)
    other_patch["patch_id"] = "patch-advance-version"
    applied = client.post("/patch/apply", json=other_patch)
    response = client.post(
        "/release/decision",
        json={
            "release_id": prepared["release_id"],
            "operator_id": "op_001",
            "decision": "APPROVE",
            "comment": "Approve stale candidate.",
        },
    )
    body = response.json()
    audit = api.repository.load_audit_bundle(body["audit_id"])

    assert applied.json()["status"] == "ACCEPTED"
    assert response.status_code == 200
    assert body["status"] == "FAILED_STALE_VERSION"
    assert validate_release_record_schema(body) == []
    assert api.repository.get_current_trt("trt-demo")["version"] == "v2"
    assert audit["status"] == "REJECTED"


def test_double_approval_blocked_for_released_release(tmp_path, fixture_loader, valid_patch):
    client = make_client(tmp_path, fixture_loader)
    prepared = prepare_release(client, valid_patch)
    first = client.post(
        "/release/decision",
        json={
            "release_id": prepared["release_id"],
            "operator_id": "op_001",
            "decision": "APPROVE",
            "comment": "Approved once.",
        },
    )

    second_approve = client.post(
        "/release/decision",
        json={
            "release_id": prepared["release_id"],
            "operator_id": "op_001",
            "decision": "APPROVE",
            "comment": "Approve again.",
        },
    )
    reject_after_release = client.post(
        "/release/decision",
        json={
            "release_id": prepared["release_id"],
            "operator_id": "op_001",
            "decision": "REJECT",
            "comment": "Reject after release.",
        },
    )

    assert first.status_code == 200
    assert first.json()["status"] == "RELEASED"
    assert second_approve.status_code == 400
    assert "not pending" in second_approve.json()["detail"]
    assert reject_after_release.status_code == 400
    assert "not pending" in reject_after_release.json()["detail"]
    assert api.repository.get_current_trt("trt-demo")["version"] == "v2"


def test_unknown_release_id_is_rejected(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.post(
        "/release/decision",
        json={
            "release_id": "rel_missing",
            "operator_id": "op_001",
            "decision": "APPROVE",
            "comment": "Unknown release.",
        },
    )

    assert response.status_code == 404
    assert "Release record not found" in response.json()["detail"]


def test_invalid_release_decision_is_rejected(tmp_path, fixture_loader, valid_patch):
    client = make_client(tmp_path, fixture_loader)
    prepared = prepare_release(client, valid_patch)

    response = client.post(
        "/release/decision",
        json={
            "release_id": prepared["release_id"],
            "operator_id": "op_001",
            "decision": "MAYBE",
            "comment": "Invalid decision.",
        },
    )

    assert response.status_code == 400
    assert "Unsupported release decision" in response.json()["detail"]
