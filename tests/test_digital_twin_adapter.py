from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from host_isaac_runner_service import (
    IsaacRunRequest,
    build_host_command,
    build_pick_up_example_args,
    finalize_successful_result_db,
    post_isaac_dry_run,
    post_isaac_run,
)
from scripts.isaac_tools_classification_helper import get_tools_classification_from_scenario
from trt_core.ent_demo import ENT_NON_MEMBER_TOOL_IDS, ENT_REQUIRED_TOOL_IDS, TARGET_SET_ID, build_tool_catalog
from trt_core import api
from trt_core.digital_twin_adapter import (
    build_isaac_command,
    build_isaac_command_args_from_scenario_spec,
    build_line_tooling,
    container_to_host_path,
    isaac_host_runtime_config,
    read_simulation_results,
    validate_scenario_spec_for_isaac,
)
from trt_core.repository import TRTRepository


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


def make_spec(tmp_path: Path) -> dict:
    return {
        "scenario_spec_id": "scn_test",
        "scenario_template_id": "surgical_sorting_data_driven_v1",
        "release_id": "rel_test",
        "trt_id": "trt-demo",
        "trt_version": "v1",
        "reconciliation_plan_id": "rec_test",
        "candidate_strategy_id": "primary",
        "workspace_contract": {
            "producer_workspace": "governance",
            "consumer_workspace": "isaac_sim",
            "exchange_mode": "file",
            "scenario_specs_dir": str(tmp_path),
            "run_artifacts_dir": str(tmp_path / "run_artifacts"),
            "expected_scenario_spec_path": str(tmp_path / "scn_test.json"),
        },
        "scene_template": "pick_up_example.py",
        "simulation_config": {
            "num_envs": 4,
            "headless": False,
            "allowed_overlap_ratio": 0.99,
            "layout_source": "auto",
            "episode_success_requires_reset_cycles": 1,
            "chosen_intervention_mode": "continue-until-arrival",
            "add_reference_number": 27,
            "reuse_verified_seed": True,
        },
        "line_bindings": [
            {"line_id": "line_1", "env_id": 0, "simulation_mode": "PHYSICAL_OR_DIGITAL_TWIN"},
            {"line_id": "line_2", "env_id": 1, "simulation_mode": "LOGICAL_ONLY"},
        ],
        "line_policies": [
            {
                "line_id": "line_1",
                "goal": "ROUTINE_CLASSIFICATION",
                "allowed_instruments": [],
                "excluded_instruments": [],
                "selected_tool_ids": ["tool_01"],
                "excluded_tool_ids": [],
                "priority": 3,
                "kpi": {"min_throughput_per_hour": 120},
                "abnormal_strategy": "CONTINUE_FEASIBLE_TASKS",
            },
            {
                "line_id": "line_2",
                "goal": "ROUTINE_CLASSIFICATION",
                "allowed_instruments": [],
                "excluded_instruments": [],
                "selected_tool_ids": [],
                "excluded_tool_ids": [],
                "priority": 3,
                "kpi": {"min_throughput_per_hour": 80},
                "abnormal_strategy": "STOP_LINE",
            },
        ],
        "tool_catalog": {
            "tool_01": {"tool_id": "tool_01", "normalized_type": "FORCEPS", "type": "Forceps"},
            "tool_02": {"tool_id": "tool_02", "normalized_type": "SCISSORS", "type": "Scissors"},
        },
        "operator_model": {},
        "abnormal_event_policy": {
            "entanglement": {
                "generation_mode": "implicit_runtime_detection",
                "enabled": True,
                "allowed_overlap_ratio": 0.25,
                "manual_event_injection": False,
                "predefined_entanglement_timestamps": [],
            }
        },
        "assertions": {},
    }


def make_ent_set_spec(tmp_path: Path) -> dict:
    spec = make_spec(tmp_path)
    spec["tool_catalog"] = build_tool_catalog()
    spec["tool_sets"] = {
        TARGET_SET_ID: {
            "set_id": TARGET_SET_ID,
            "required_tool_ids": list(ENT_REQUIRED_TOOL_IDS),
            "non_member_tool_ids": list(ENT_NON_MEMBER_TOOL_IDS),
        }
    }
    spec["line_bindings"] = [
        {"line_id": "line_1", "env_id": 0, "simulation_mode": "PHYSICAL_OR_DIGITAL_TWIN"},
        {"line_id": "line_2", "env_id": 1, "simulation_mode": "PHYSICAL_OR_DIGITAL_TWIN"},
        {"line_id": "line_3", "env_id": 2, "simulation_mode": "LOGICAL_ONLY"},
        {"line_id": "line_4", "env_id": 3, "simulation_mode": "LOGICAL_ONLY"},
    ]
    spec["line_policies"] = [
        {
            "line_id": line_id,
            "target_set_id": TARGET_SET_ID,
            "selected_tool_ids": [],
            "excluded_tool_ids": [],
            "tooling_policy": {"required_scope": "NONE"},
        }
        for line_id in ("line_1", "line_2", "line_3", "line_4")
    ]
    return spec


def test_validate_scenario_spec_accepts_current_isaac_contract(tmp_path):
    spec = make_spec(tmp_path)
    assert validate_scenario_spec_for_isaac(spec) == []


def test_selected_and_unselected_tooling_are_per_line(tmp_path):
    tooling = build_line_tooling(make_spec(tmp_path))
    assert tooling["line_1"]["selected_tools"] == ["tool_01"]
    assert tooling["line_1"]["unselected_tools"] == ["tool_02"]
    assert tooling["line_2"]["selected_tools"] == []
    assert tooling["line_2"]["unselected_tools"] == ["tool_01", "tool_02"]
    assert tooling["line_1"]["selected_tools"] is not tooling["line_2"]["selected_tools"]


def test_ent_target_set_classification_uses_set_membership_not_empty_selected_tool_ids(tmp_path):
    tooling = build_line_tooling(make_ent_set_spec(tmp_path))

    assert tooling["line_1"]["selected_tools"] == list(ENT_REQUIRED_TOOL_IDS)
    assert tooling["line_1"]["unselected_tools"] == list(ENT_NON_MEMBER_TOOL_IDS)
    assert tooling["line_2"]["selected_tools"] == list(ENT_REQUIRED_TOOL_IDS)
    assert tooling["line_2"]["unselected_tools"] == list(ENT_NON_MEMBER_TOOL_IDS)


def test_tools_classification_helper_returns_ent_tool_numbers_per_line(tmp_path):
    selected, unselected = get_tools_classification_from_scenario(make_ent_set_spec(tmp_path), "line_1")

    assert selected == [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20, 21, 23, 24, 25, 26, 27]
    assert unselected == [1, 2, 3, 4, 5, 18, 22]


def test_ent_target_set_classification_moves_line_exclusions_to_unwanted_tools(tmp_path):
    spec = make_ent_set_spec(tmp_path)
    spec["line_policies"][2]["excluded_tool_ids"] = ["tool_16", "tool_17", "tool_18"]
    spec["line_policies"][3]["excluded_tool_ids"] = ["tool_16", "tool_17", "tool_18"]

    line_3_selected, line_3_unselected = get_tools_classification_from_scenario(spec, "line_3")
    line_1_selected, line_1_unselected = get_tools_classification_from_scenario(spec, "line_1")

    assert 16 not in line_3_selected
    assert 17 not in line_3_selected
    assert line_3_unselected == [1, 2, 3, 4, 5, 18, 22, 16, 17]
    assert line_1_selected == [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20, 21, 23, 24, 25, 26, 27]
    assert line_1_unselected == [1, 2, 3, 4, 5, 18, 22]


def test_tools_classification_helper_requires_line_id_for_multi_line_scenario(tmp_path):
    try:
        get_tools_classification_from_scenario(make_ent_set_spec(tmp_path), None)
    except ValueError as exc:
        assert str(exc) == "line_id is required when ScenarioSpec contains multiple production lines."
    else:
        raise AssertionError("Expected line_id requirement for multi-line ScenarioSpec")


def test_command_builder_generates_host_runner_request(monkeypatch, tmp_path):
    spec = make_spec(tmp_path)
    spec_path = tmp_path / "scn_test.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("ISAAC_HOST_RUNNER_URL", "http://host-runner.test")
    monkeypatch.setenv("HOST_PROJECT_ROOT", r"C:\Project")
    monkeypatch.setenv("CONTAINER_PROJECT_ROOT", str(tmp_path).replace("\\", "/"))
    command = build_isaac_command(spec, TRTRepository(tmp_path), scenario_spec_path=spec_path)
    host_request = command["host_request"]
    assert command["execution_mode"] == "host_runner"
    assert "command" not in command
    assert host_request["python_bat"].endswith(r"\_build\windows-x86_64\release\python.bat")
    assert host_request["entry_script"].endswith(r"\pick_up_example.py")
    assert host_request["scenario_spec_path"].startswith(r"C:\Project")
    assert host_request["command_args"] == {
        "num_envs": 4,
        "headless": False,
        "allowed_overlap_ratio": 0.99,
        "layout_source": "auto",
        "episode_success_requires_reset_cycles": 1,
        "chosen_intervention_mode": "continue-until-arrival",
        "travel_time": 5.0,
        "fix_duration": 8.0,
        "resume_delay": 0.5,
        "add_reference_number": 27,
        "reuse_verified_seed": True,
    }
    assert command["validation_errors"] == []


def test_scenario_spec_command_args_override_host_runner_defaults(tmp_path):
    spec = make_spec(tmp_path)
    spec["simulation_config"] = {
        "num_envs": 9,
        "headless": False,
        "global_seed": 777,
        "max_seed_trials": 3,
        "allowed_overlap_ratio": 0.42,
        "layout_source": "online",
        "episode_success_requires_reset_cycles": 2,
        "chosen_intervention_mode": "immediate-stop",
        "add_reference_number": 27,
        "reuse_verified_seed": False,
        "reuse_precomputed_layouts": True,
    }
    spec["operator_model"] = {
        "travel_time": 11,
        "fix_duration": 22,
        "resume_delay": 0.7,
    }

    from trt_core.digital_twin_adapter.isaac_command_builder import build_isaac_command_args_with_sources

    resolved = build_isaac_command_args_with_sources(spec)
    args = resolved["command_args"]

    assert args["num_envs"] == 9
    assert args["headless"] is False
    assert args["global_seed"] == 777
    assert "max_seed_trials" not in args
    assert args["allowed_overlap_ratio"] == 0.42
    assert args["layout_source"] == "online"
    assert args["episode_success_requires_reset_cycles"] == 2
    assert args["chosen_intervention_mode"] == "immediate-stop"
    assert args["travel_time"] == 11.0
    assert args["fix_duration"] == 22.0
    assert args["resume_delay"] == 0.7
    assert args["add_reference_number"] == 27
    assert args["reuse_verified_seed"] is False
    assert "reuse_precomputed_layouts" not in args
    assert resolved["resolved_from"]["num_envs"] == "scenario_spec.simulation_config.num_envs"
    assert resolved["resolved_from"]["global_seed"] == "scenario_spec.simulation_config.global_seed"
    assert resolved["resolved_from"]["travel_time"] == "scenario_spec.operator_model.travel_time"
    assert resolved["resolved_from"]["max_seed_trials"] == "omitted: restricted"
    assert resolved["resolved_from"]["reuse_precomputed_layouts"] == "omitted: restricted"


def test_scenario_spec_command_args_fallback_to_line_bindings_and_tool_catalog(tmp_path):
    spec = make_spec(tmp_path)
    spec["simulation_config"].pop("num_envs", None)
    spec["simulation_config"].pop("add_reference_number", None)
    spec["line_bindings"] = [
        {"line_id": "line_1", "enabled": True},
        {"line_id": "line_2", "enabled": True},
        {"line_id": "line_3", "enabled": False},
    ]
    spec["tool_catalog"] = {f"tool_{index:02d}": {"tool_id": f"tool_{index:02d}"} for index in range(1, 6)}

    args = build_isaac_command_args_from_scenario_spec(spec)

    assert args["num_envs"] == 2
    assert args["add_reference_number"] == 5


def test_scenario_spec_command_args_prefer_simulation_scope_lines_over_affected_lines(tmp_path):
    spec = make_spec(tmp_path)
    spec["affected_lines"] = ["line_3", "line_4"]
    spec["simulation_scope"] = {
        "mode": "FULL_SYSTEM_DEFAULT",
        "lines": ["line_1", "line_2", "line_3", "line_4"],
        "reason": "Full-system simulation is required by default because the Time-Arrival Model is a system-level variable.",
    }
    spec["simulation_config"].pop("num_envs", None)
    spec["line_bindings"] = [
        {"line_id": "line_1", "enabled": True},
        {"line_id": "line_2", "enabled": True},
        {"line_id": "line_3", "enabled": True},
        {"line_id": "line_4", "enabled": True},
    ]

    resolved = build_isaac_command_args_from_scenario_spec(spec)

    assert resolved["num_envs"] == 4


def test_scenario_spec_command_args_allow_explicit_operator_limited_scope(tmp_path):
    spec = make_spec(tmp_path)
    spec["affected_lines"] = ["line_3", "line_4"]
    spec["simulation_scope"] = {
        "mode": "EXPLICIT_OPERATOR_LIMITED",
        "lines": ["line_3", "line_4"],
        "reason": "Operator explicitly requested a reduced simulation scope.",
    }
    spec["simulation_config"].pop("num_envs", None)

    resolved = build_isaac_command_args_from_scenario_spec(spec)

    assert resolved["num_envs"] == 2


def test_path_mapper_preserves_literal_dollar_username_from_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "data"
    config_dir.mkdir()
    config_dir.joinpath("isaac_host_config.json").write_text(
        json.dumps(
            {
                "host_project_root": r"C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system",
                "container_project_root": "/app",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOST_PROJECT_ROOT", r"C:\Users-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system")

    mapped = container_to_host_path("/app/outputs/scenario_specs/scn_x.json", TRTRepository(tmp_path))
    config = isaac_host_runtime_config(TRTRepository(tmp_path))

    assert mapped == r"C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system\outputs\scenario_specs\scn_x.json"
    assert config["host_project_root_source"] == "config_file"
    assert config["warnings"] == [
        "HOST_PROJECT_ROOT appears to have lost a literal dollar-sign username segment. Use data/isaac_host_config.json or escape $ as $$ in Compose."
    ]


def test_path_mapper_uses_env_when_config_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOST_PROJECT_ROOT", r"C:\Project")
    monkeypatch.setenv("CONTAINER_PROJECT_ROOT", "/app")

    mapped = container_to_host_path("/app/outputs/scenario_specs/scn_x.json", TRTRepository(tmp_path))
    config = isaac_host_runtime_config(TRTRepository(tmp_path))

    assert mapped == r"C:\Project\outputs\scenario_specs\scn_x.json"
    assert config["host_project_root_source"] == "env"


def test_host_runner_command_targets_pick_up_entry(monkeypatch, tmp_path):
    fake_script = Path(__file__).parent / "fixtures" / "fake_pick_up_example.py"
    fake_python = tmp_path / "python.bat"
    fake_python.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("ISAAC_UR5_ENTRY_SCRIPT", str(fake_script))
    monkeypatch.setenv("ISAAC_PYTHON_BAT", str(fake_python))
    request = IsaacRunRequest(
        scenario_spec_id="scn_test",
        scenario_spec_path=str(tmp_path / "scenario.json"),
        output_db_path=str(tmp_path / "run.sqlite"),
        run_id="sim_host_test",
        python_bat=str(fake_python),
        entry_script=str(fake_script),
        working_directory=str(tmp_path),
    )
    command = build_host_command(request)
    assert command["command"][0].endswith("python.bat")
    assert command["command"][1].endswith("fake_pick_up_example.py")
    assert command["command"][2:] == [
        "--num_envs",
        "4",
        "--headless",
        "false",
        "--layout_source",
        "auto",
        "--episode_success_requires_reset_cycles",
        "1",
        "--allowed_overlap_ratio",
        "0.99",
        "--chosen_intervention_mode",
        "continue-until-arrival",
        "--travel_time",
        "5.0",
        "--fix_duration",
        "8.0",
        "--resume_delay",
        "0.5",
        "--add_reference_number",
        "27",
        "--run_id",
        "sim_host_test",
        "--output_db_path",
        str(tmp_path / "run.sqlite"),
        "--reuse_verified_seed",
    ]


def test_pick_up_example_arg_mapping_omits_false_store_true_and_empty_seed_path(tmp_path):
    host_request = {
        "command_args": {
            "num_envs": 4,
            "headless": False,
            "layout_source": "auto",
            "episode_success_requires_reset_cycles": 1,
            "allowed_overlap_ratio": 0.99,
            "chosen_intervention_mode": "continue-until-arrival",
            "travel_time": 5,
            "fix_duration": 8,
            "resume_delay": 0.5,
            "add_reference_number": 27,
            "reuse_verified_seed": True,
        }
    }
    args = build_pick_up_example_args(host_request)
    assert ["--num_envs", "4"] == args[0:2]
    assert "--headless" in args
    assert "--global_seed" not in args
    assert "--max_seed_trials" not in args
    assert "--chosen_intervention_mode" in args
    assert "--travel_time" in args
    assert "--fix_duration" in args
    assert "--resume_delay" in args
    assert "--reuse_verified_seed" in args
    assert "--reuse_precomputed_layouts" not in args
    assert "--seed_db_path" not in args


def test_pick_up_example_arg_mapping_global_seed_disables_reuse_verified_seed(tmp_path):
    host_request = {
        "command_args": {
            "num_envs": 4,
            "headless": False,
            "global_seed": 777,
            "layout_source": "auto",
            "episode_success_requires_reset_cycles": 1,
            "allowed_overlap_ratio": 0.99,
            "chosen_intervention_mode": "continue-until-arrival",
            "travel_time": 5,
            "fix_duration": 8,
            "resume_delay": 0.5,
            "add_reference_number": 27,
            "reuse_verified_seed": True,
        }
    }
    args = build_pick_up_example_args(host_request)

    assert "--global_seed" in args
    assert args[args.index("--global_seed") + 1] == "777"
    assert "--reuse_verified_seed" not in args


def test_pick_up_example_arg_mapping_includes_existing_seed_db_path(tmp_path):
    seed_db = tmp_path / "seed_sweep.sqlite3"
    seed_db.write_text("", encoding="utf-8")
    output_db = tmp_path / "sim.sqlite"
    host_request = {
        "output_db_path": str(output_db),
        "command_args": {
            "seed_db_path": str(seed_db),
        },
    }
    args = build_pick_up_example_args(host_request)

    assert "--seed_db_path" in args
    assert args[args.index("--seed_db_path") + 1] == str(seed_db)
    assert "--output_db_path" in args
    assert args[args.index("--output_db_path") + 1] == str(output_db)
    assert args[args.index("--seed_db_path") + 1] != args[args.index("--output_db_path") + 1]


def test_host_runner_completed_process_without_output_db_reports_artifact_failure(monkeypatch, tmp_path):
    fake_script = Path(__file__).parent / "fixtures" / "fake_pick_up_example.py"
    fake_python = tmp_path / "python.bat"
    fake_python.write_text("@echo off\n", encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text("{}", encoding="utf-8")
    seed_db = tmp_path / "seed_sweep.sqlite3"
    seed_db.write_text("", encoding="utf-8")

    class Completed:
        returncode = 0
        stdout = "isaac finished"
        stderr = ""

    def fake_run(*args, **kwargs):
        return Completed()

    monkeypatch.setattr("host_isaac_runner_service.subprocess.run", fake_run)
    request = IsaacRunRequest(
        scenario_spec_id="scn_test",
        scenario_spec_path=str(scenario_path),
        output_db_path=str(tmp_path / "missing_result.sqlite"),
        run_id="sim_no_db",
        python_bat=str(fake_python),
        entry_script=str(fake_script),
        working_directory=str(tmp_path),
        command_args={"seed_db_path": str(seed_db)},
    )

    result = post_isaac_run(request)

    assert result["status"] == "COMPLETED_NO_RESULT_DB"
    assert result["return_code"] == 0
    assert result["seed_db_path"] == str(seed_db)
    assert result["output_db_path"].endswith("missing_result.sqlite")
    assert result["output_db_exists"] is False
    assert "did not produce the result DB" in result["errors"][0]


def test_host_runner_finalizes_running_output_db_after_clean_exit(monkeypatch, tmp_path):
    fake_script = Path(__file__).parent / "fixtures" / "fake_pick_up_example.py"
    fake_python = tmp_path / "python.bat"
    fake_python.write_text("@echo off\n", encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text("{}", encoding="utf-8")
    output_db = tmp_path / "running_result.sqlite"

    class Completed:
        returncode = 0
        stdout = "isaac finished"
        stderr = ""

    def fake_run(*args, **kwargs):
        create_running_result_db(output_db, "sim_running_host")
        return Completed()

    monkeypatch.setattr("host_isaac_runner_service.subprocess.run", fake_run)
    request = IsaacRunRequest(
        scenario_spec_id="scn_test",
        scenario_spec_path=str(scenario_path),
        output_db_path=str(output_db),
        run_id="sim_running_host",
        python_bat=str(fake_python),
        entry_script=str(fake_script),
        working_directory=str(tmp_path),
        command_args={"num_envs": 4},
    )

    result = post_isaac_run(request)
    artifact = read_simulation_results(output_db, "sim_running_host")

    assert result["status"] == "COMPLETED"
    assert result["return_code"] == 0
    assert result["result_db_diagnostics"]["result_db_finalized_by_host_runner"] is True
    assert result["result_db_diagnostics"]["result_db_status_before"] == "RUNNING"
    assert result["result_db_diagnostics"]["result_db_status_after"] == "COMPLETED"
    assert result["result_db_diagnostics"]["line_kpis_count"] == 4
    assert artifact["status"] == "COMPLETED"
    assert artifact["run"]["completed_at"] is not None
    assert len(artifact["line_kpis"]) == 4


def test_host_runner_result_db_finalizer_commits_for_separate_reader(tmp_path):
    output_db = tmp_path / "manual_finalize.sqlite"
    create_running_result_db(output_db, "sim_commit")

    diagnostics = finalize_successful_result_db(
        {
            "run_id": "sim_commit",
            "scenario_spec_id": "scn_commit",
            "scenario_spec_path": "spec.json",
            "output_db_path": str(output_db),
            "command_args": {"num_envs": 2},
        }
    )
    artifact = read_simulation_results(output_db, "sim_commit")

    assert diagnostics["result_db_finalized_by_host_runner"] is True
    assert diagnostics["line_kpis_count"] == 2
    assert artifact["status"] == "COMPLETED"
    assert len(artifact["line_kpis"]) == 2


def test_host_runner_dry_run_returns_command_without_launching(tmp_path):
    fake_script = Path(__file__).parent / "fixtures" / "fake_pick_up_example.py"
    fake_python = tmp_path / "python.bat"
    fake_python.write_text("@echo off\n", encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text("{}", encoding="utf-8")
    request = IsaacRunRequest(
        scenario_spec_id="scn_test",
        scenario_spec_path=str(scenario_path),
        output_db_path=str(tmp_path / "run.sqlite"),
        run_id="sim_dry_run",
        python_bat=str(fake_python),
        entry_script=str(fake_script),
        working_directory=str(tmp_path),
    )
    result = post_isaac_dry_run(request)
    assert result["status"] == "READY"
    assert result["command"][0] == str(fake_python)
    assert result["command"][1] == str(fake_script)
    assert result["missing_paths"] == []


def test_host_runner_dry_run_rejects_invalid_layout_source(tmp_path):
    fake_script = Path(__file__).parent / "fixtures" / "fake_pick_up_example.py"
    fake_python = tmp_path / "python.bat"
    fake_python.write_text("@echo off\n", encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text("{}", encoding="utf-8")
    request = IsaacRunRequest(
        scenario_spec_id="scn_test",
        scenario_spec_path=str(scenario_path),
        output_db_path=str(tmp_path / "run.sqlite"),
        run_id="sim_bad_layout",
        python_bat=str(fake_python),
        entry_script=str(fake_script),
        working_directory=str(tmp_path),
        command_args={"layout_source": "unsupported"},
    )
    result = post_isaac_dry_run(request)
    assert result["status"] == "FAILED"
    assert "Unsupported layout_source: unsupported" in result["errors"]


def test_host_runner_dry_run_rejects_invalid_intervention_mode(tmp_path):
    fake_script = Path(__file__).parent / "fixtures" / "fake_pick_up_example.py"
    fake_python = tmp_path / "python.bat"
    fake_python.write_text("@echo off\n", encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text("{}", encoding="utf-8")
    request = IsaacRunRequest(
        scenario_spec_id="scn_test",
        scenario_spec_path=str(scenario_path),
        output_db_path=str(tmp_path / "run.sqlite"),
        run_id="sim_bad_mode",
        python_bat=str(fake_python),
        entry_script=str(fake_script),
        working_directory=str(tmp_path),
        command_args={"chosen_intervention_mode": "unknown"},
    )
    result = post_isaac_dry_run(request)
    assert result["status"] == "FAILED"
    assert "Unsupported chosen_intervention_mode: unknown" in result["errors"]


def test_host_runner_missing_seed_db_is_warning_not_failure(tmp_path):
    fake_script = Path(__file__).parent / "fixtures" / "fake_pick_up_example.py"
    fake_python = tmp_path / "python.bat"
    fake_python.write_text("@echo off\n", encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text("{}", encoding="utf-8")
    request = IsaacRunRequest(
        scenario_spec_id="scn_test",
        scenario_spec_path=str(scenario_path),
        output_db_path=str(tmp_path / "run.sqlite"),
        run_id="sim_missing_seed",
        python_bat=str(fake_python),
        entry_script=str(fake_script),
        working_directory=str(tmp_path),
        command_args={"seed_db_path": str(tmp_path / "missing_seed.sqlite3")},
    )
    result = post_isaac_dry_run(request)

    assert result["status"] == "READY"
    assert "--seed_db_path" not in result["command"]
    assert any("seed_db_path does not exist" in warning for warning in result["warnings"])


def create_result_db(path: Path, run_id: str):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE simulation_runs(run_id TEXT PRIMARY KEY, scenario_spec_id TEXT, scenario_spec_path TEXT, started_at TEXT, completed_at TEXT, status TEXT, error_message TEXT);
            CREATE TABLE line_kpis(run_id TEXT, line_id TEXT, throughput_per_hour REAL, completed_count INTEGER, wanted_completed_count INTEGER, unwanted_completed_count INTEGER, misplaced_count INTEGER, entanglement_count INTEGER, downtime_seconds REAL, cycle_time_seconds REAL, success INTEGER);
            CREATE TABLE tool_events(run_id TEXT, line_id TEXT, tool_id TEXT, tool_type TEXT, wanted INTEGER, picked INTEGER, placed INTEGER, placement_target TEXT, placement_correct INTEGER, event_time_seconds REAL);
            """
        )
        connection.execute("INSERT INTO simulation_runs VALUES (?, ?, ?, ?, ?, ?, ?)", (run_id, "scn", "spec.json", "a", "b", "COMPLETED", None))
        connection.execute("INSERT INTO line_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (run_id, "line_1", 120, 2, 1, 1, 0, 0, 0, 1, 1))
        connection.execute("INSERT INTO tool_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (run_id, "line_1", "tool_01", "FORCEPS", 1, 1, 1, "wanted_tray", 1, 0))


def create_running_result_db(path: Path, run_id: str):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE simulation_runs(run_id TEXT PRIMARY KEY, scenario_spec_id TEXT, scenario_spec_path TEXT, started_at TEXT, completed_at TEXT, status TEXT, error_message TEXT);
            CREATE TABLE line_kpis(run_id TEXT, line_id TEXT, throughput_per_hour REAL, completed_count INTEGER, wanted_completed_count INTEGER, unwanted_completed_count INTEGER, misplaced_count INTEGER, entanglement_count INTEGER, downtime_seconds REAL, cycle_time_seconds REAL, success INTEGER);
            CREATE TABLE tool_events(run_id TEXT, line_id TEXT, tool_id TEXT, tool_type TEXT, wanted INTEGER, picked INTEGER, placed INTEGER, placement_target TEXT, placement_correct INTEGER, event_time_seconds REAL);
            """
        )
        connection.execute("INSERT INTO simulation_runs VALUES (?, ?, ?, ?, ?, ?, ?)", (run_id, "scn", "spec.json", "a", None, "RUNNING", None))


def test_result_reader_reads_sqlite(tmp_path):
    db_path = tmp_path / "results.sqlite"
    create_result_db(db_path, "sim_test")
    result = read_simulation_results(db_path, "sim_test")
    assert result["summary"]["total_completed"] == 2
    assert result["summary"]["overall_success"] is True
    assert result["tool_events"][0]["tool_id"] == "tool_01"


def test_result_reader_reports_running_result_not_finalized(tmp_path):
    db_path = tmp_path / "running.sqlite"
    create_running_result_db(db_path, "sim_running")
    result = read_simulation_results(db_path, "sim_running")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "SIMULATION_RESULT_NOT_FINALIZED"
    assert result["message"] == "Isaac exited successfully, but the result DB was left in RUNNING state."
    assert result["simulation_run_status"] == "RUNNING"
    assert result["completed_at"] is None
    assert result["line_kpis_count"] == 0
    assert result["tool_events_count"] == 0


def test_simulation_scope_result_validation_requires_kpis_for_all_simulated_lines(tmp_path):
    from trt_core.api import _simulation_scope_result_error

    spec = make_spec(tmp_path)
    spec["simulation_scope"] = {
        "mode": "FULL_SYSTEM_DEFAULT",
        "lines": ["line_1", "line_2", "line_3", "line_4"],
        "reason": "Full-system simulation is required by default because the Time-Arrival Model is a system-level variable.",
    }
    artifact = {"status": "COMPLETED", "line_kpis_count": 2, "line_kpis": [{"line_id": "line_1"}, {"line_id": "line_2"}]}

    error = _simulation_scope_result_error(spec, artifact)

    assert error["error_code"] == "SIMULATION_RESULT_SCOPE_MISMATCH"
    assert error["expected_line_kpis_count"] == 4
    assert error["line_kpis_count"] == 2


def test_result_reader_controlled_errors(tmp_path):
    assert read_simulation_results(tmp_path / "missing.sqlite", "sim")["error_code"] == "SIMULATION_DB_NOT_FOUND"
    invalid = tmp_path / "invalid.sqlite"
    with sqlite3.connect(invalid) as connection:
        connection.execute("CREATE TABLE simulation_runs(run_id TEXT)")
    assert read_simulation_results(invalid, "sim")["error_code"] == "SIMULATION_DB_SCHEMA_INVALID"
    db_path = tmp_path / "results.sqlite"
    create_result_db(db_path, "sim_other")
    assert read_simulation_results(db_path, "sim_missing")["error_code"] == "SIMULATION_RUN_NOT_FOUND"


def test_tools_classification_helper_is_line_specific(tmp_path):
    spec = make_spec(tmp_path)
    line_1_selected, line_1_unselected = get_tools_classification_from_scenario(spec, "line_1")
    line_2_selected, line_2_unselected = get_tools_classification_from_scenario(spec, "line_2")
    assert line_1_selected == [1]
    assert line_1_unselected == [2]
    assert line_2_selected == []
    assert line_2_unselected == [1, 2]
    assert line_1_selected is not line_2_selected


def test_simulation_run_without_host_url_returns_controlled_failure_without_fake_command(monkeypatch, tmp_path):
    spec = make_spec(tmp_path)
    spec_path = tmp_path / "scn_test.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.delenv("ISAAC_HOST_RUNNER_URL", raising=False)
    client = TestClient(api.app)

    response = client.post(
        "/simulation/run",
        json={
            "scenario_spec_id": "scn_test",
            "scenario_spec_path": str(spec_path),
            "run_mode": "SYNC",
            "headless": True,
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "FAILED"
    assert body["errors"] == [HOST_RUNNER_NOT_CONFIGURED_MESSAGE]
    assert body["setup_diagnostics"] == HOST_RUNNER_SETUP_DIAGNOSTICS
    assert "command" not in body
    assert body["execution_mode"] == "host_runner"


def test_isaac_host_runner_status_reports_missing_url(monkeypatch):
    monkeypatch.delenv("ISAAC_HOST_RUNNER_URL", raising=False)
    client = TestClient(api.app)

    response = client.get("/debug/isaac-host-runner-status")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "MISSING_URL"
    assert body["host_runner_url_configured"] is False
    assert body["available"] is False
    assert body["errors"] == [HOST_RUNNER_NOT_CONFIGURED_MESSAGE]
    assert body["setup_diagnostics"] == HOST_RUNNER_SETUP_DIAGNOSTICS


def test_isaac_host_runner_status_reports_health(monkeypatch):
    monkeypatch.setenv("ISAAC_HOST_RUNNER_URL", "http://host-runner.test")

    def fake_get_isaac_health(base_url, timeout_seconds=5):
        assert base_url == "http://host-runner.test"
        return {
            "status": "OK",
            "service": "host_isaac_runner",
            "python_bat_exists": True,
            "entry_script_exists": True,
            "working_directory_exists": True,
        }

    def fake_post_isaac_dry_run(base_url, payload, timeout_seconds=5):
        assert base_url == "http://host-runner.test"
        return {"status": "READY", "errors": [], "missing_paths": []}

    monkeypatch.setattr(api, "get_isaac_health", fake_get_isaac_health)
    monkeypatch.setattr(api, "post_isaac_dry_run", fake_post_isaac_dry_run)
    client = TestClient(api.app)

    response = client.get("/debug/isaac-host-runner-status")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "OK"
    assert body["host_runner_url_configured"] is True
    assert body["available"] is True
    assert body["python_bat_exists"] is True
    assert body["entry_script_exists"] is True
    assert body["working_directory_exists"] is True
    assert body["sample_path_exists_via_host_runner"] is True
    assert body["errors"] == []
    assert body["setup_diagnostics"] == []


def test_isaac_command_preview_returns_host_request_without_launching(monkeypatch, tmp_path):
    spec = make_spec(tmp_path)
    spec_path = tmp_path / "scn_test.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("ISAAC_HOST_RUNNER_URL", "http://host-runner.test")
    monkeypatch.setenv("HOST_PROJECT_ROOT", r"C:\Project")
    monkeypatch.setenv("CONTAINER_PROJECT_ROOT", str(tmp_path).replace("\\", "/"))
    client = TestClient(api.app)

    response = client.post(
        "/debug/isaac-command-preview",
        json={
            "scenario_spec_id": "scn_test",
            "scenario_spec_path": str(spec_path),
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "READY"
    assert body["execution_mode"] == "host_runner"
    assert body["container_scenario_spec_path"] == str(spec_path)
    assert body["host_scenario_spec_path"] == body["host_request"]["scenario_spec_path"]
    assert body["expected_command_args"]["num_envs"] == 4
    assert body["expected_command_args"]["headless"] is False
    assert "global_seed" not in body["expected_command_args"]
    assert "max_seed_trials" not in body["expected_command_args"]
    assert body["expected_command_args"]["chosen_intervention_mode"] == "continue-until-arrival"
    assert "reuse_precomputed_layouts" not in body["expected_command_args"]
    assert body["expected_command_args"]["seed_db_path"]
    assert body["arg_provenance"]["seed_db_path"] == "host_config.seed_db_path"


def test_simulation_run_endpoint_uses_host_runner(monkeypatch, tmp_path):
    spec = make_spec(tmp_path)
    spec_path = tmp_path / "scn_test.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("ISAAC_HOST_RUNNER_URL", "http://host-runner.test")
    monkeypatch.setenv("ISAAC_RESULT_TRANSPORT", "host_api")
    monkeypatch.setenv("HOST_PROJECT_ROOT", r"C:\Project")
    monkeypatch.setenv("CONTAINER_PROJECT_ROOT", str(tmp_path).replace("\\", "/"))

    def fake_post_isaac_run(base_url, payload, timeout_seconds=600):
        return {
            "run_id": payload["run_id"],
            "status": "COMPLETED",
            "output_db_path": payload["output_db_path"],
            "stdout_tail": "fake host completed",
            "stderr_tail": "",
            "errors": [],
            "return_code": 0,
        }

    def fake_get_isaac_result(base_url, run_id, timeout_seconds=600):
        return {
            "run_id": run_id,
            "line_kpis": [],
            "tool_events": [],
            "summary": {
                "total_completed": 4,
                "total_wanted_completed": 1,
                "total_unwanted_completed": 3,
                "total_entanglements": 0,
                "total_downtime_seconds": 0,
                "overall_success": True,
            },
        }

    monkeypatch.setattr(api, "post_isaac_run", fake_post_isaac_run)
    monkeypatch.setattr(api, "get_isaac_result", fake_get_isaac_result)

    client = TestClient(api.app)
    response = client.post(
        "/simulation/run",
        json={
            "scenario_spec_id": "scn_test",
            "scenario_spec_path": str(spec_path),
            "run_mode": "SYNC",
            "headless": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["kpis"]["total_completed"] == 4
    assert body["run_artifact"]["summary"]["total_wanted_completed"] == 1
    assert body["host_runner"]["stdout_tail"] == "fake host completed"
    assert body["execution_mode"] == "host_runner"
    assert body["host_request"]["scenario_spec_path"].startswith(r"C:\Project")
    assert body["host_request"]["command_args"]["layout_source"] == "auto"


def test_simulation_run_missing_host_scenario_path_skips_result_reader(monkeypatch, tmp_path):
    spec = make_spec(tmp_path)
    spec_path = tmp_path / "scn_test.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("ISAAC_HOST_RUNNER_URL", "http://host-runner.test")
    monkeypatch.setenv("ISAAC_RESULT_TRANSPORT", "shared_db")
    monkeypatch.setenv("HOST_PROJECT_ROOT", r"C:\Project")
    monkeypatch.setenv("CONTAINER_PROJECT_ROOT", str(tmp_path).replace("\\", "/"))

    def fake_post_isaac_run(base_url, payload, timeout_seconds=600):
        return {
            "run_id": payload["run_id"],
            "status": "FAILED",
            "output_db_path": payload["output_db_path"],
            "stdout_tail": "",
            "stderr_tail": "",
            "errors": [f"ScenarioSpec path does not exist: {payload['scenario_spec_path']}"],
            "missing_paths": [f"ScenarioSpec path does not exist: {payload['scenario_spec_path']}"],
            "return_code": None,
        }

    def fail_read_results(*args, **kwargs):
        raise AssertionError("result reader should not run when Isaac never launched")

    monkeypatch.setattr(api, "post_isaac_run", fake_post_isaac_run)
    monkeypatch.setattr(api, "read_simulation_results", fail_read_results)

    client = TestClient(api.app)
    response = client.post(
        "/simulation/run",
        json={
            "scenario_spec_id": "scn_test",
            "scenario_spec_path": str(spec_path),
            "run_mode": "SYNC",
            "headless": True,
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "FAILED"
    assert body["error_code"] == "SCENARIO_SPEC_HOST_PATH_NOT_FOUND"
    assert body["run_artifact"] is None
    assert body["result_transport"] is None
    assert body["container_scenario_spec_path"] == str(spec_path)
    assert "host_scenario_spec_path" in body


def test_simulation_run_completed_without_result_db_returns_specific_error(monkeypatch, tmp_path):
    spec = make_spec(tmp_path)
    spec_path = tmp_path / "scn_test.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("ISAAC_HOST_RUNNER_URL", "http://host-runner.test")
    monkeypatch.setenv("ISAAC_RESULT_TRANSPORT", "shared_db")
    monkeypatch.setenv("HOST_PROJECT_ROOT", r"C:\Project")
    monkeypatch.setenv("CONTAINER_PROJECT_ROOT", str(tmp_path).replace("\\", "/"))

    def fake_post_isaac_run(base_url, payload, timeout_seconds=600):
        return {
            "run_id": payload["run_id"],
            "status": "COMPLETED_NO_RESULT_DB",
            "output_db_path": payload["output_db_path"],
            "seed_db_path": payload["command_args"].get("seed_db_path"),
            "stdout_tail": "isaac shutdown cleanly",
            "stderr_tail": "",
            "errors": ["Isaac completed successfully but did not produce the result DB."],
            "return_code": 0,
            "command": ["python.bat", "pick_up_example.py", "--output_db_path", payload["output_db_path"]],
        }

    def fail_read_results(*args, **kwargs):
        raise AssertionError("result reader should not run when host runner reports missing output DB")

    monkeypatch.setattr(api, "post_isaac_run", fake_post_isaac_run)
    monkeypatch.setattr(api, "read_simulation_results", fail_read_results)

    client = TestClient(api.app)
    response = client.post(
        "/simulation/run",
        json={
            "scenario_spec_id": "scn_test",
            "scenario_spec_path": str(spec_path),
            "run_mode": "SYNC",
            "headless": True,
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "FAILED"
    assert body["error_code"] == "SIMULATION_COMPLETED_BUT_RESULT_DB_MISSING"
    assert body["return_code"] == 0
    assert body["host_output_db_path"].startswith(r"C:\Project")
    assert body["seed_db_path"]
    assert body["run_artifact"] is None
    assert body["result_transport"] is None
    assert body["note"] == "seed_sweep.sqlite3 is an input DB, not the result DB."


def test_simulation_run_running_result_db_returns_not_finalized(monkeypatch, tmp_path):
    spec = make_spec(tmp_path)
    spec_path = tmp_path / "scn_test.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("ISAAC_HOST_RUNNER_URL", "http://host-runner.test")
    monkeypatch.setenv("ISAAC_RESULT_TRANSPORT", "shared_db")
    monkeypatch.setenv("HOST_PROJECT_ROOT", r"C:\Project")
    monkeypatch.setenv("CONTAINER_PROJECT_ROOT", str(tmp_path).replace("\\", "/"))

    def fake_post_isaac_run(base_url, payload, timeout_seconds=600):
        return {
            "run_id": payload["run_id"],
            "status": "COMPLETED",
            "output_db_path": payload["output_db_path"],
            "stdout_tail": "isaac shutdown cleanly",
            "stderr_tail": "",
            "errors": [],
            "return_code": 0,
        }

    def fake_read_results(db_path, run_id):
        return {
            "status": "ERROR",
            "error_code": "SIMULATION_RESULT_NOT_FINALIZED",
            "message": "Isaac exited successfully, but the result DB was left in RUNNING state.",
            "run_id": run_id,
            "db_path": str(db_path),
            "simulation_run_status": "RUNNING",
            "completed_at": None,
            "line_kpis_count": 0,
            "tool_events_count": 0,
            "summary": {"overall_success": False},
        }

    monkeypatch.setattr(api, "post_isaac_run", fake_post_isaac_run)
    monkeypatch.setattr(api, "read_simulation_results", fake_read_results)

    client = TestClient(api.app)
    response = client.post(
        "/simulation/run",
        json={
            "scenario_spec_id": "scn_test",
            "scenario_spec_path": str(spec_path),
            "run_mode": "SYNC",
            "headless": True,
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "FAILED"
    assert body["error_code"] == "SIMULATION_RESULT_NOT_FINALIZED"
    assert body["result_diagnostics"]["simulation_run_status"] == "RUNNING"
    assert body["result_diagnostics"]["completed_at"] is None
    assert body["result_diagnostics"]["line_kpis_count"] == 0
    assert body["result_diagnostics"]["host_runner_return_code"] == 0
