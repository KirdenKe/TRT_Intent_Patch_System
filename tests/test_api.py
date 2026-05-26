from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient

import trt_core.api as api
from trt_core.repository import TRTRepository


def make_client(tmp_path, fixture_loader) -> TestClient:
    repository = TRTRepository(tmp_path)
    repository.save_trt(fixture_loader("trt_v1.json"))
    api.repository = repository
    return TestClient(api.app)


def test_get_current_trt_returns_current_trt(tmp_path, fixture_loader):
    client = make_client(tmp_path, fixture_loader)

    response = client.get("/trt/current")

    assert response.status_code == 200
    assert response.json()["trt_id"] == "trt-demo"
    assert response.json()["version"] == "v1"


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
