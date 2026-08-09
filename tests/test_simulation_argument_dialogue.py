from __future__ import annotations

import pytest

from trt_core.digital_twin_adapter.isaac_command_builder import build_isaac_command_args_with_sources
from trt_core.intent_normalizer import (
    normalize_domain_candidate,
    parse_tooling_count_request,
    relative_time_arrival_validation_errors,
)


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


def test_dry_run_time_arrival_updates_are_reviewed_and_compile_to_command_args():
    data = candidate("milestone 11.6 dry run")
    data.update(
        {
            "target_scope": "MULTIPLE_LINES",
            "target_lines": ["line_1", "line_2"],
            "request_types": ["SIMULATION_CONFIG_UPDATE", "ABNORMAL_STRATEGY_UPDATE", "DRY_RUN_ONLY"],
            "simulation_config_updates": {
                "num_envs": 2,
                "chosen_intervention_mode": "immediate-stop",
                "travel_time": 3.0,
                "fix_duration": 6.0,
                "resume_delay": 1.5,
                "add_reference_number": 6,
                "dry_run_only": True,
            },
            "dry_run_only": True,
            "deployment_allowed_after_success": False,
        }
    )

    patch = normalize_domain_candidate(data, current_trt())

    assert patch["operations"] == []
    assert patch["dry_run_only"] is True
    assert patch["deployment_allowed_after_success"] is False
    assert patch["simulation_config_updates"] == data["simulation_config_updates"]

    spec = {
        "simulation_scope": {"mode": "EXPLICIT_OPERATOR_LIMITED", "lines": ["line_1", "line_2"]},
        "simulation_config": patch["simulation_config_updates"],
        "operator_model": {"travel_time": 5.0, "fix_duration": 8.0, "resume_delay": 0.5},
        "tool_catalog": {f"tool_{index:02d}": {} for index in range(1, 28)},
        "governance_metadata": {
            "dry_run_only": True,
            "expected_command_args": {
                "num_envs": 2,
                "chosen_intervention_mode": "immediate-stop",
                "travel_time": 3.0,
                "fix_duration": 6.0,
                "resume_delay": 1.5,
                "add_reference_number": 6,
            },
        },
    }
    args = build_isaac_command_args_with_sources(spec)["command_args"]

    assert args["num_envs"] == 2
    assert args["chosen_intervention_mode"] == "immediate-stop"
    assert args["travel_time"] == 3.0
    assert args["fix_duration"] == 6.0
    assert args["resume_delay"] == 1.5
    assert args["add_reference_number"] == 6


def test_relative_time_arrival_request_accepts_gemma_derived_values():
    text = (
        "with two production lines remaining, reduce the current arrival time by 0.5 seconds, "
        "reduce the current entanglement fix time by 1 second, make the current recovery delay "
        "1 second slower, and simulate 4 tooling per line"
    )
    data = candidate(text)
    data.update(
        {
            "request_types": ["SIMULATION_CONFIG_UPDATE"],
            "simulation_config_updates": {
                "num_envs": 2,
                "travel_time": 0.5,
                "fix_duration": 2,
                "resume_delay": 2,
                "add_reference_number": 4,
            },
        }
    )

    patch = normalize_domain_candidate(
        data,
        current_trt(),
        time_arrival_baseline={
            "travel_time": 1.0,
            "fix_duration": 3.0,
            "resume_delay": 1.0,
        },
    )

    assert patch["simulation_config_updates"] == {
        "num_envs": 2,
        "travel_time": 0.5,
        "fix_duration": 2.0,
        "resume_delay": 2.0,
        "add_reference_number": 4,
    }


def test_simulate_tooling_per_line_phrase_is_an_explicit_simulation_value():
    text = "simulate 4 tooling per line"

    assert parse_tooling_count_request(text) == {"add_reference_number": 4}
    errors = relative_time_arrival_validation_errors(
        {
            "intent_text": text,
            "simulation_config_updates": {"num_envs": 2},
        },
        None,
    )

    assert errors == ["Gemma omitted the derived add_reference_number value"]


def test_relative_time_arrival_request_rejects_incorrect_gemma_derivation():
    text = (
        "with two production lines remaining, reduce the current arrival time by 0.5 seconds, "
        "reduce the current entanglement fix time by 1 second, make the current recovery delay "
        "1 second slower, and simulate 4 tooling per line"
    )
    data = candidate(text)
    data.update(
        {
            "request_types": ["SIMULATION_CONFIG_UPDATE"],
            "simulation_config_updates": {
                "num_envs": 2,
                "travel_time": 0,
                "fix_duration": 2,
                "resume_delay": 2,
                "add_reference_number": 4,
            },
        }
    )

    with pytest.raises(ValueError, match="Gemma derived an inconsistent travel_time value"):
        normalize_domain_candidate(
            data,
            current_trt(),
            time_arrival_baseline={
                "travel_time": 1.0,
                "fix_duration": 3.0,
                "resume_delay": 1.0,
            },
        )


def test_exact_time_arrival_request_preserves_valid_model_derivation():
    text = (
        "okay, two production lines fucked up today. i want to confirm that with only two production lines "
        "remaining, my arrival time can be reduced by about 2.5 seconds, and the time to resolve entanglements "
        "can be reduced by 1.5 seconds. however, to ensure the remaining production lines operate normally, "
        "pls stop the robotic arms immediately upon detecting an anomaly. because of this, pls adjust the "
        "recovery time to be 2 second slower, and set the number of tooling per production line to 5. adjust "
        "the throughput/hr for all production lines to at least 90; set the tooling picking target for "
        "production lines 2 to knife handle; and adjust the tooling picking order for production lines 1 to "
        "prioritize picking tooling other than ent tooling set."
    )
    data = candidate(text)
    data.update(
        {
            "request_types": ["SIMULATION_CONFIG_UPDATE"],
            "simulation_config_updates": {
                "num_envs": 2,
                "chosen_intervention_mode": "immediate-stop",
                "travel_time": 2.5,
                "fix_duration": 6.5,
                "resume_delay": 2.5,
                "add_reference_number": 5,
            },
        }
    )

    patch = normalize_domain_candidate(
        data,
        current_trt(),
        time_arrival_baseline={
            "travel_time": 5.0,
            "fix_duration": 8.0,
            "resume_delay": 0.5,
        },
    )

    assert patch["simulation_config_updates"] == {
        "num_envs": 2,
        "chosen_intervention_mode": "immediate-stop",
        "travel_time": 2.5,
        "fix_duration": 6.5,
        "resume_delay": 2.5,
        "add_reference_number": 5,
    }

    spec = {
        "simulation_scope": {"mode": "EXPLICIT_OPERATOR_LIMITED", "lines": ["line_1", "line_2"]},
        "simulation_config": patch["simulation_config_updates"],
        "operator_model": {"travel_time": 5.0, "fix_duration": 8.0, "resume_delay": 0.5},
        "tool_catalog": {f"tool_{index:02d}": {} for index in range(1, 28)},
        "governance_metadata": {"expected_command_args": patch["simulation_config_updates"]},
    }
    args = build_isaac_command_args_with_sources(spec)["command_args"]

    assert args["num_envs"] == 2
    assert args["chosen_intervention_mode"] == "immediate-stop"
    assert args["travel_time"] == 2.5
    assert args["fix_duration"] == 6.5
    assert args["resume_delay"] == 2.5
    assert args["add_reference_number"] == 5


def test_explicit_simulation_values_reject_inconsistent_model_output():
    text = (
        "today there are two production lines. my arrival time is 4 seconds, the time required "
        "to resolve the tangling issue is 5 seconds, and the recovery time is 0.5 seconds. "
        "continue until i arrive at the production line. the allow overlap ratio is 0.9"
    )
    data = candidate(text)
    data["simulation_config_updates"] = {
        "num_envs": 2,
        "travel_time": 3,
        "fix_duration": 2,
        "resume_delay": 1.5,
    }

    with pytest.raises(ValueError, match="inconsistent travel_time"):
        normalize_domain_candidate(
            data,
            current_trt(),
            time_arrival_baseline={
                "travel_time": 1.0,
                "fix_duration": 3.0,
                "resume_delay": 1.0,
            },
        )


def test_explicit_simulation_values_accept_complete_consistent_model_output():
    text = (
        "today there are two production lines. my arrival time is 4 seconds, the time required "
        "to resolve the tangling issue is 5 seconds, and the recovery time is 0.5 seconds. "
        "continue until i arrive at the production line. the allow overlap ratio is 0.9"
    )
    data = candidate(text)
    data.update(
        {
            "request_types": ["SIMULATION_CONFIG_UPDATE"],
            "simulation_config_updates": {
                "num_envs": 2,
                "travel_time": 4.0,
                "fix_duration": 5.0,
                "resume_delay": 0.5,
                "chosen_intervention_mode": "continue-until-arrival",
                "allowed_overlap_ratio": 0.9,
            },
        }
    )

    patch = normalize_domain_candidate(
        data,
        current_trt(),
        time_arrival_baseline={
            "travel_time": 1.0,
            "fix_duration": 3.0,
            "resume_delay": 1.0,
        },
    )

    assert patch["simulation_config_updates"] == data["simulation_config_updates"]


def test_intervention_mode_format_variant_is_canonicalized_without_regeneration():
    text = "continue until i arrive at the production line"
    data = candidate(text)
    data.update(
        {
            "request_types": ["SIMULATION_CONFIG_UPDATE"],
            "simulation_config_updates": {
                "chosen_intervention_mode": "CONTINUE_UNTIL_ARRIVAL"
            },
        }
    )

    patch = normalize_domain_candidate(data, current_trt())

    assert patch["simulation_config_updates"]["chosen_intervention_mode"] == (
        "continue-until-arrival"
    )
