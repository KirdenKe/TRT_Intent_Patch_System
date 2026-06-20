from __future__ import annotations

import json
from copy import deepcopy

from fastapi.testclient import TestClient

import trt_core.api as api
from scripts.generate_ent_demo_state import generate
from trt_core.ent_demo import (
    ENT_NON_MEMBER_TOOL_IDS,
    ENT_REQUIRED_TOOL_IDS,
    SUPPORTED_TOOL_IDS,
    build_current_state,
    build_trt,
    state_object_to_records,
)
from trt_core.line_registry import load_line_registry
from trt_core.intent_normalizer import normalize_domain_candidate
from trt_core.repository import TRTRepository
from trt_core.semantic_rules import validate_semantics
from trt_core.validator import validate_trt_schema


def test_ent_demo_has_four_lines_and_expected_tool_membership_counts():
    trt = build_trt()

    assert sorted(trt["lines"]) == ["line_1", "line_2", "line_3", "line_4"]
    assert len(trt["tool_catalog"]) == 27
    assert len(trt["tool_sets"]["ENT_SURGICAL_TOOLING_SET"]["required_tool_ids"]) == 20
    assert len(trt["tool_sets"]["ENT_SURGICAL_TOOLING_SET"]["non_member_tool_ids"]) == 7
    assert trt["tool_sets"]["ENT_SURGICAL_TOOLING_SET"]["required_tool_ids"] == ENT_REQUIRED_TOOL_IDS
    assert trt["tool_sets"]["ENT_SURGICAL_TOOLING_SET"]["non_member_tool_ids"] == ENT_NON_MEMBER_TOOL_IDS


def test_duplicate_instrument_types_remain_distinct_tool_ids():
    catalog = build_trt()["tool_catalog"]

    assert catalog["tool_07"]["normalized_type"] == "NEEDLE_HOLDER"
    assert catalog["tool_08"]["normalized_type"] == "NEEDLE_HOLDER"
    assert catalog["tool_07"]["tool_id"] != catalog["tool_08"]["tool_id"]
    forceps_ids = [tool_id for tool_id, tool in catalog.items() if tool["normalized_type"] == "FORCEPS"]
    assert len(forceps_ids) > 1


def test_selected_and_excluded_tool_ids_empty_are_valid():
    trt = build_trt()

    for line in trt["lines"].values():
        assert line["selected_tool_ids"] == []
        assert line["excluded_tool_ids"] == []
    assert validate_trt_schema(trt) == []
    assert validate_semantics(trt) == []


def test_entanglement_runtime_state_does_not_change_policy_tool_ids():
    trt = build_trt()
    state = build_current_state()
    state["lines"]["line_1"]["entanglement"] = {
        "detected": True,
        "tool_ids": ["tool_07", "tool_08"],
        "severity": "moderate",
        "requires_operator": True,
    }

    assert trt["lines"]["line_1"]["selected_tool_ids"] == []
    assert trt["lines"]["line_1"]["excluded_tool_ids"] == []
    records = state_object_to_records(state)
    assert records[0]["entanglement"]["tool_ids"] == ["tool_07", "tool_08"]


def test_digital_twin_mapping_fields_exist_for_all_lines():
    trt = build_trt()
    registry = load_line_registry()

    for index, line_id in enumerate(["line_1", "line_2", "line_3", "line_4"]):
        mapping = trt["lines"][line_id]["digital_twin"]
        binding = registry["lines"][line_id]
        assert mapping["robot_id"] == f"ur5_{line_id}"
        assert mapping["robot_model"] == "UR5"
        assert mapping["robot_scene_name"] == f"ur5_robot_{index}"
        assert mapping["workspace_id"] == f"workspace_{line_id}"
        assert mapping["workspace_env_id"] == index
        assert mapping["stage_robot_prim_path"] == binding["stage_robot_prim_path"]
        assert mapping["tray_id"] == binding["tray_id"]
        assert mapping["stage_end_effector_prim_path"].startswith(binding["stage_robot_prim_path"])
        assert mapping["stage_tooling_root_prim_path"].startswith(binding["stage_robot_prim_path"].rsplit("/", 1)[0])


def test_generated_json_files_are_accepted_by_api_validation(tmp_path):
    paths = generate(tmp_path)
    trt = json.loads((tmp_path / "data" / "trt_versions" / "trt-demo_v1.json").read_text(encoding="utf-8"))
    mirrored_trt = json.loads((tmp_path / "data" / "trt" / "trt-demo_v1.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / "data" / "state_records" / "current_state.json").read_text(encoding="utf-8"))
    repository = TRTRepository(tmp_path)

    assert paths["current_pointer_path"].endswith("data\\trt\\current_trt.json") or paths[
        "current_pointer_path"
    ].endswith("data/trt/current_trt.json")
    assert trt == mirrored_trt
    assert validate_trt_schema(trt) == []
    assert validate_semantics(trt) == []
    assert repository.get_current_trt("trt-demo")["version"] == "v1"
    assert len(repository.load_state_records()) == 4
    assert state["lines"]["line_4"]["robot_id"] == "ur5_line_4"


def test_intent_context_exposes_valid_target_sets_and_aliases():
    body = api.build_intent_context(build_trt())

    assert body["valid_target_set_ids"] == ["ENT_SURGICAL_TOOLING_SET"]
    assert body["target_set_aliases"]["ent surgical tooling set"] == "ENT_SURGICAL_TOOLING_SET"
    assert body["target_set_aliases"]["ent set"] == "ENT_SURGICAL_TOOLING_SET"
    assert body["llm_candidate_generation_schema"]["properties"]["target_set_id"]["enum"] == [
        "ENT_SURGICAL_TOOLING_SET",
        None,
    ]


def test_reset_ent_demo_state_updates_current_state_object_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    generate(tmp_path)
    repository = TRTRepository(tmp_path)
    api.repository = repository
    client = TestClient(api.app)
    state_path = tmp_path / "data" / "state_records" / "current_state.json"
    dirty_state = build_current_state()
    dirty_state["lines"]["line_2"]["mode"] = "ERROR"
    dirty_state["lines"]["line_2"]["last_exception"] = "jam_detected"
    dirty_state["lines"]["line_2"]["selected_tool_ids"] = ["tool_07"]
    state_path.write_text(json.dumps(dirty_state, indent=2), encoding="utf-8")

    response = client.post("/debug/reset-ent-demo-state")

    assert response.status_code == 200
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["lines"]["line_2"]["mode"] == "RUNNING"
    assert persisted["lines"]["line_2"]["last_exception"] is None
    assert persisted["lines"]["line_2"]["selected_tool_ids"] == []
    assert persisted["lines"]["line_2"]["entanglement"]["detected"] is False
    assert len(response.json()["state_records"]) == 4


def test_intent_normalizer_prefers_instance_level_tool_ids_for_ent_set_selection():
    trt = build_trt()
    candidate = {
        "patch_id": "patch-ent-select-all",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "Select all ENT set tooling for line 1.",
        "reason": "ENT set sorting run",
        "line_id": "line_1",
        "target_scope": "SINGLE_LINE",
        "target_lines": ["line_1"],
        "request_types": ["INSTRUMENT_SCOPE_UPDATE", "TOOLING_POLICY_UPDATE"],
        "goal": None,
        "priority": None,
        "target_set_id": "ENT_SURGICAL_TOOLING_SET",
        "selected_tool_ids": list(ENT_REQUIRED_TOOL_IDS),
        "excluded_tool_ids": [],
        "required_tool_ids": list(ENT_REQUIRED_TOOL_IDS),
        "allowed_instruments": None,
        "excluded_instruments": None,
        "kpi_updates": None,
        "tooling_policy": {"required_scope": "SELECTED_TOOLING"},
        "abnormal_strategy": None,
        "status": "REVIEWED",
    }

    intent_patch = normalize_domain_candidate(candidate, trt)

    assert {"op": "replace", "path": "/lines/line_1/selected_tool_ids", "value": ENT_REQUIRED_TOOL_IDS} in intent_patch[
        "operations"
    ]
    assert {"op": "replace", "path": "/lines/line_1/excluded_tool_ids", "value": []} in intent_patch["operations"]
    assert not any(operation["path"] == "/lines/line_1/allowed_instruments" for operation in intent_patch["operations"])


def test_legacy_allowed_instruments_translate_to_instance_tool_ids():
    trt = build_trt()
    candidate = {
        "patch_id": "patch-legacy-forceps",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "Select forceps for line 1.",
        "reason": "legacy compatibility",
        "line_id": "line_1",
        "goal": None,
        "allowed_instruments": ["FORCEPS"],
        "excluded_instruments": None,
        "status": "REVIEWED",
    }

    intent_patch = normalize_domain_candidate(candidate, trt)
    expected_forceps_tool_ids = [
        tool_id for tool_id, tool in trt["tool_catalog"].items() if tool["normalized_type"] == "FORCEPS"
    ]

    assert {
        "op": "replace",
        "path": "/lines/line_1/selected_tool_ids",
        "value": expected_forceps_tool_ids,
    } in intent_patch["operations"]


def test_remove_knife_handles_from_lines_three_and_four_is_reviewable_instrument_scope_update():
    trt = build_trt()
    candidate = {
        "patch_id": "patch-remove-knife-handles",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "remove the knife handles from production lines 3 and 4",
        "reason": "operator requested line-specific tooling exclusion",
        "line_id": None,
        "target_scope": None,
        "target_lines": None,
        "request_types": None,
        "goal": None,
        "priority": None,
        "allowed_instruments": None,
        "excluded_instruments": None,
        "selected_tool_ids": None,
        "excluded_tool_ids": None,
        "required_tool_ids": None,
        "target_set_id": None,
        "kpi_updates": None,
        "tooling_policy": None,
        "abnormal_strategy": None,
        "clarification_questions": [],
        "unsupported_terms": [],
        "detected_request_types": None,
        "status": "REVIEWED",
    }

    intent_patch = normalize_domain_candidate(candidate, trt)

    assert intent_patch["status"] == "REVIEWED"
    assert {"op": "replace", "path": "/lines/line_3/excluded_tool_ids", "value": ["tool_16", "tool_17", "tool_18"]} in intent_patch[
        "operations"
    ]
    assert {"op": "replace", "path": "/lines/line_4/excluded_tool_ids", "value": ["tool_16", "tool_17", "tool_18"]} in intent_patch[
        "operations"
    ]
    assert not any(operation["path"].startswith("/lines/line_1/") for operation in intent_patch["operations"])
    assert not any(operation["path"].startswith("/lines/line_2/") for operation in intent_patch["operations"])
    assert not any(operation["path"].endswith("/goal") for operation in intent_patch["operations"])


def test_target_surgical_set_update_for_all_lines_does_not_require_goal():
    trt = build_trt()
    candidate = {
        "patch_id": "patch-target-ent-set",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "adjust the targets for all production lines to the ENT surgical tooling set",
        "reason": "operator requested ENT set target",
        "line_id": None,
        "target_scope": None,
        "target_lines": None,
        "request_types": None,
        "goal": None,
        "priority": None,
        "allowed_instruments": None,
        "excluded_instruments": None,
        "selected_tool_ids": None,
        "excluded_tool_ids": None,
        "required_tool_ids": None,
        "target_set_id": None,
        "kpi_updates": None,
        "tooling_policy": None,
        "abnormal_strategy": None,
        "clarification_questions": ["Please specify which goal you would like to apply."],
        "unsupported_terms": [],
        "detected_request_types": None,
        "status": "REVIEWED",
    }

    intent_patch = normalize_domain_candidate(candidate, trt)

    assert intent_patch["operations"] == [
        {"op": "replace", "path": f"/lines/line_{index}/target_set_id", "value": "ENT_SURGICAL_TOOLING_SET"}
        for index in range(1, 5)
    ]
    assert not any(operation["path"].endswith("/goal") for operation in intent_patch["operations"])


def test_target_set_aliases_map_to_ent_surgical_tooling_set():
    trt = build_trt()
    aliases = ["ENT set", "ENT tooling set", "ENT surgical set"]
    for alias in aliases:
        candidate = {
            "patch_id": f"patch-{alias.lower().replace(' ', '-')}",
            "trt_id": "trt-demo",
            "base_version": "v1",
            "operator_id": "op_001",
            "intent_text": f"use {alias} as the target set for every line",
            "reason": "operator requested ENT set target",
            "line_id": None,
            "target_scope": None,
            "target_lines": None,
            "request_types": None,
            "goal": None,
            "priority": None,
            "allowed_instruments": None,
            "excluded_instruments": None,
            "selected_tool_ids": None,
            "excluded_tool_ids": None,
            "required_tool_ids": None,
            "target_set_id": None,
            "kpi_updates": None,
            "tooling_policy": None,
            "abnormal_strategy": None,
            "clarification_questions": [],
            "unsupported_terms": [],
            "detected_request_types": None,
            "status": "REVIEWED",
        }

        intent_patch = normalize_domain_candidate(candidate, trt)

        assert all(operation["value"] == "ENT_SURGICAL_TOOLING_SET" for operation in intent_patch["operations"])
        assert not any(operation["path"].endswith("/goal") for operation in intent_patch["operations"])


def test_all_supported_tooling_scope_selects_all_27_tool_ids():
    trt = build_trt()
    candidate = {
        "patch_id": "patch-all-supported-tooling",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": "All supported tooling for line 1.",
        "reason": "full tool sweep",
        "line_id": "line_1",
        "goal": None,
        "allowed_instruments": None,
        "excluded_instruments": None,
        "tooling_policy": {"required_scope": "ALL_SUPPORTED_TOOLING"},
        "status": "REVIEWED",
    }

    intent_patch = normalize_domain_candidate(candidate, trt)

    assert {"op": "replace", "path": "/lines/line_1/selected_tool_ids", "value": SUPPORTED_TOOL_IDS} in intent_patch[
        "operations"
    ]
    assert {"op": "replace", "path": "/lines/line_1/excluded_tool_ids", "value": []} in intent_patch["operations"]
