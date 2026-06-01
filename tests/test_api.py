from __future__ import annotations

import json
from copy import deepcopy

from fastapi.testclient import TestClient

import trt_core.api as api
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
    assert body["scenario_spec_path"] == f"outputs/scenario_specs/{body['scenario_spec_id']}.json"
    assert (tmp_path / body["scenario_spec_path"]).exists()
    assert not (tmp_path / "exchange" / "scenario_specs").exists()
    assert body["scenario_spec"]["release_id"] == "rel_api_scenario_001"
    assert body["scenario_spec"]["workspace_contract"]["exchange_mode"] == "file"
    assert "outputs/scenario_specs" in body["scenario_spec"]["workspace_contract"]["expected_scenario_spec_path"]
    assert "outputs/run_artifacts" in body["scenario_spec"]["workspace_contract"]["expected_run_artifact_path"]


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
        "goal",
        "excluded_instruments",
        "clarification_questions",
        "unsupported_terms",
        "detected_request_types",
    ]
    assert set(body["llm_candidate_generation_schema"]["properties"]) == {
        "action",
        "line_id",
        "goal",
        "excluded_instruments",
        "clarification_questions",
        "unsupported_terms",
        "detected_request_types",
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
    assert "operations" not in body["llm_candidate_generation_schema"]["properties"]
    assert body["domain_candidate_internal_schema"]["required"] == [
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
