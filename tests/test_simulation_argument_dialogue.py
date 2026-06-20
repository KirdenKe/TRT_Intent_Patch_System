from __future__ import annotations

import pytest

from trt_core.digital_twin_adapter.isaac_command_builder import build_isaac_command_args_with_sources
from trt_core.intent_normalizer import normalize_domain_candidate


def current_trt() -> dict:
    return {
        "trt_id": "trt-demo",
        "version": "v1",
        "tool_catalog": {f"tool_{index:02d}": {"tool_id": f"tool_{index:02d}"} for index in range(1, 28)},
        "lines": {
            f"line_{index}": {
                "state": {"mode": "RUNNING", "last_exception": None},
            }
            for index in range(1, 5)
        },
    }


def candidate(intent_text: str) -> dict:
    return {
        "patch_id": "patch-sim-arg-test",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": intent_text,
        "reason": "dialogue test",
        "line_id": None,
        "target_scope": None,
        "target_lines": None,
        "request_types": None,
        "goal": None,
        "priority": None,
        "allowed_instruments": None,
        "excluded_instruments": None,
        "selected_normalized_types": None,
        "excluded_normalized_types": None,
        "selected_tool_ids": None,
        "excluded_tool_ids": None,
        "required_tool_ids": None,
        "target_set_id": None,
        "simulation_config_updates": None,
        "kpi_updates": {},
        "tooling_policy": None,
        "abnormal_strategy": None,
        "clarification_questions": [],
        "unsupported_terms": [],
        "detected_request_types": None,
        "status": "REVIEWED",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("run the simulation in headless mode", {"headless": True}),
        ("run the simulation with rendering enabled", {"headless": False}),
        ("use global seed 777 for this simulation", {"global_seed": 777, "reuse_verified_seed": False}),
        ("require 3 successful reset cycles for the simulation", {"episode_success_requires_reset_cycles": 3}),
        ("set the allowed overlap ratio to 0.75", {"allowed_overlap_ratio": 0.75}),
        ("use immediate stop as the intervention mode", {"chosen_intervention_mode": "immediate-stop"}),
        ("continue until operator arrival when intervention is needed", {"chosen_intervention_mode": "continue-until-arrival"}),
        ("set operator travel time to 12 seconds", {"travel_time": 12}),
        ("set the entanglement fix duration to 10 seconds", {"fix_duration": 10}),
        ("set resume delay to 1.5 seconds", {"resume_delay": 1.5}),
        ("set add_reference_number to 5 for the simulation", {"add_reference_number": 5}),
    ],
)
def test_allowed_simulation_arguments_are_mapped(text: str, expected: dict):
    patch = normalize_domain_candidate(candidate(text), current_trt())

    assert patch["operations"] == []
    assert patch["status"] == "REVIEWED"
    assert "SIMULATION_CONFIG_UPDATE" in patch["request_types"]
    assert patch["simulation_config_updates"] == expected
    assert "goal" not in patch


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "set max_seed_trials to 10",
            "max_seed_trials is an internal developer sweep parameter and cannot be changed through normal operator requests.",
        ),
        (
            r"set seed_db_path to C:\temp\seed.sqlite3",
            "seed_db_path is infrastructure configuration and cannot be changed through normal operator requests.",
        ),
        (
            "enable reuse_precomputed_layouts",
            "reuse_precomputed_layouts is an internal layout-cache setting and cannot be changed through normal operator requests.",
        ),
        (
            "set layout_source to database",
            "layout_source is an infrastructure simulation setting and cannot be changed through normal operator requests.",
        ),
    ],
)
def test_restricted_simulation_arguments_are_rejected(text: str, message: str):
    with pytest.raises(ValueError, match=message):
        normalize_domain_candidate(candidate(text), current_trt())


def test_multi_turn_clarification_resolves_add_reference_number():
    text = (
        "adjust the number of tooling on the production line so that only 5 remain "
        "Clarification: i mean all, the add_reference_number argument"
    )

    patch = normalize_domain_candidate(candidate(text), current_trt())

    assert patch["simulation_config_updates"] == {"add_reference_number": 5}
    assert patch["operations"] == []
    assert "specific tools" not in patch["message"].lower()


def test_command_defaults_and_global_seed_rule():
    spec = {
        "simulation_scope": {"lines": ["line_1", "line_2", "line_3", "line_4"]},
        "simulation_config": {
            "headless": False,
            "layout_source": "auto",
            "episode_success_requires_reset_cycles": 1,
            "allowed_overlap_ratio": 0.99,
            "chosen_intervention_mode": "continue-until-arrival",
            "travel_time": 5.0,
            "fix_duration": 8.0,
            "resume_delay": 0.5,
            "add_reference_number": 27,
            "reuse_verified_seed": True,
        },
        "tool_catalog": {f"tool_{index:02d}": {} for index in range(1, 28)},
    }

    resolved = build_isaac_command_args_with_sources(spec)
    args = resolved["command_args"]

    assert args["num_envs"] == 4
    assert args["headless"] is False
    assert args["layout_source"] == "auto"
    assert args["episode_success_requires_reset_cycles"] == 1
    assert args["allowed_overlap_ratio"] == 0.99
    assert args["chosen_intervention_mode"] == "continue-until-arrival"
    assert args["travel_time"] == 5.0
    assert args["fix_duration"] == 8.0
    assert args["resume_delay"] == 0.5
    assert args["add_reference_number"] == 27
    assert args["reuse_verified_seed"] is True
    assert "global_seed" not in args
    assert "max_seed_trials" not in args
    assert "reuse_precomputed_layouts" not in args
    assert resolved["resolved_from"]["global_seed"] == "omitted: no operator override"

    spec["simulation_config"]["global_seed"] = 777
    seeded = build_isaac_command_args_with_sources(spec)["command_args"]
    assert seeded["global_seed"] == 777
    assert seeded["reuse_verified_seed"] is False
