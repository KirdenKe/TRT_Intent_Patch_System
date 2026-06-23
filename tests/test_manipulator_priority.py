import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from scenario_generation.generator import generate_scenario_spec
from scenario_generation.models import ScenarioGenerationRequest
from trt_core.digital_twin_adapter.result_reader import read_simulation_results
from trt_core.intent_precheck import deterministic_intent_precheck
from trt_core.intent_normalizer import normalize_domain_candidate
from trt_core.intent_normalizer import parse_tooling_count_request
from trt_core.validator import validate_firewall


def current_trt() -> dict:
    required = [
        "tool_06",
        "tool_07",
        "tool_08",
        "tool_09",
        "tool_10",
        "tool_11",
        "tool_12",
        "tool_13",
        "tool_14",
        "tool_15",
        "tool_16",
        "tool_17",
        "tool_19",
        "tool_20",
        "tool_21",
        "tool_23",
        "tool_24",
        "tool_25",
        "tool_26",
        "tool_27",
    ]
    non_members = ["tool_01", "tool_02", "tool_03", "tool_04", "tool_05", "tool_18", "tool_22"]
    catalog = {
        f"tool_{index:02d}": {
            "tool_id": f"tool_{index:02d}",
            "tool_number": index,
            "type": "Knife Handle" if index in {16, 17, 18} else "Forceps",
            "normalized_type": "KNIFE_HANDLE" if index in {16, 17, 18} else "FORCEPS",
            "belongs_to_ent_set": f"tool_{index:02d}" in required,
            "set_id": "ENT_SURGICAL_TOOLING_SET" if f"tool_{index:02d}" in required else None,
            "quantity_instance": 1,
        }
        for index in range(1, 28)
    }
    lines = {}
    for index in range(1, 5):
        lines[f"line_{index}"] = {
            "goal": "ROUTINE_CLASSIFICATION",
            "allowed_instruments": [],
            "excluded_instruments": [],
            "selected_tool_ids": [],
            "excluded_tool_ids": [],
            "required_tool_ids": [],
            "target_set_id": "ENT_SURGICAL_TOOLING_SET",
            "priority": 3,
            "kpi": {
                "deadline_minutes": None,
                "max_downtime_seconds": None,
                "min_throughput_per_hour": 120,
            },
            "abnormal_strategy": "CONTINUE_FEASIBLE_TASKS",
            "tooling_policy": {"required_scope": "SELECTED_TOOLING"},
        }
    return {
        "trt_id": "trt-demo",
        "version": "v1",
        "tool_sets": {
            "ENT_SURGICAL_TOOLING_SET": {
                "set_id": "ENT_SURGICAL_TOOLING_SET",
                "required_tool_ids": required,
                "non_member_tool_ids": non_members,
            }
        },
        "tool_catalog": catalog,
        "lines": lines,
    }


def candidate(intent_text: str) -> dict:
    return {
        "patch_id": "patch-test",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "intent_text": intent_text,
        "reason": "test",
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
        "manipulator_priority": None,
        "simulation_config_updates": None,
        "kpi_updates": {},
        "tooling_policy": None,
        "abnormal_strategy": None,
        "clarification_questions": [],
        "unsupported_terms": [],
        "detected_request_types": None,
        "status": "REVIEWED",
    }


def test_required_first_all_lines_generates_manipulator_priority_patch():
    patch = normalize_domain_candidate(
        candidate("make all robots pick required tools first operator_id: op_001 reason: milestone 10 test"),
        current_trt(),
    )

    assert "MANIPULATOR_PRIORITY_UPDATE" in patch["request_types"]
    assert patch["affected_lines"] == ["line_1", "line_2", "line_3", "line_4"]
    assert all(operation["path"].endswith("/manipulator_priority") for operation in patch["operations"])
    assert all(operation["value"]["policy"] == "REQUIRED_FIRST" for operation in patch["operations"])
    assert not any(operation["path"].endswith("/priority") for operation in patch["operations"])


def test_required_first_lines_1_and_3_generates_manipulator_priority_patch():
    patch = normalize_domain_candidate(
        candidate("make production lines 1 and 3 pick ENT required tools first operator_id: op_001 reason: milestone 10 test"),
        current_trt(),
    )

    assert patch["affected_lines"] == ["line_1", "line_3"]
    assert "MANIPULATOR_PRIORITY_UPDATE" in patch["request_types"]
    assert [operation["path"] for operation in patch["operations"]] == [
        "/lines/line_1/manipulator_priority",
        "/lines/line_3/manipulator_priority",
    ]
    assert all(operation["value"]["policy"] == "REQUIRED_FIRST" for operation in patch["operations"])
    assert all(operation["value"]["enabled"] is True for operation in patch["operations"])


def test_focus_on_ent_set_maps_to_target_set_and_required_first_priority():
    patch = normalize_domain_candidate(
        candidate("prioritize production lines 1 and 3 to focus on the ENT surgical tooling set operator_id: op_001 reason: milestone 10 test"),
        current_trt(),
    )

    assert patch["affected_lines"] == ["line_1", "line_3"]
    assert "TOOLING_POLICY_UPDATE" in patch["request_types"]
    assert "MANIPULATOR_PRIORITY_UPDATE" in patch["request_types"]
    paths = [operation["path"] for operation in patch["operations"]]
    assert "/lines/line_1/target_set_id" in paths
    assert "/lines/line_3/target_set_id" in paths
    priority_ops = [operation for operation in patch["operations"] if operation["path"].endswith("/manipulator_priority")]
    assert len(priority_ops) == 2
    assert all(operation["value"]["policy"] == "REQUIRED_FIRST" for operation in priority_ops)


def test_combined_adjustment_focus_request_still_creates_manipulator_priority():
    patch = normalize_domain_candidate(
        candidate(
            "prioritize the adjustment of production lines 1 and 3 to focus on the ent surgical tooling set, "
            "and adjust the number of tooling on the production line so that only 6 remain "
            "operator_id: op_001 reason: milestone 10 validation"
        ),
        current_trt(),
    )

    assert patch["affected_lines"] == ["line_1", "line_3"]
    assert "MANIPULATOR_PRIORITY_UPDATE" in patch["request_types"]
    assert "SIMULATION_CONFIG_UPDATE" in patch["request_types"]
    assert patch["simulation_config_updates"] == {"add_reference_number": 6}
    priority_ops = [operation for operation in patch["operations"] if operation["path"].endswith("/manipulator_priority")]
    assert len(priority_ops) == 2
    assert all(operation["value"]["policy"] == "REQUIRED_FIRST" for operation in priority_ops)
    assert "simulated tooling count to 6" in patch["message"]
    assert "add_reference_number" not in patch["message"]


def test_composite_request_sub_requests_compile_independently():
    trt = current_trt()
    trt["tool_catalog"]["tool_09"]["type"] = "Surgical Forceps"
    trt["tool_catalog"]["tool_09"]["normalized_type"] = "SURGICAL_FORCEPS"
    trt["tool_catalog"]["tool_10"]["type"] = "Sponge Forceps"
    trt["tool_catalog"]["tool_10"]["normalized_type"] = "SPONGE_FORCEPS"
    trt["tool_catalog"]["tool_20"]["type"] = "Nerve Retractor"
    trt["tool_catalog"]["tool_20"]["normalized_type"] = "NERVE_RETRACTOR"
    trt["tool_catalog"]["tool_21"]["type"] = "Double-ended Surgical Retractor"
    trt["tool_catalog"]["tool_21"]["normalized_type"] = "DOUBLE_ENDED_SURGICAL_RETRACTOR"
    trt["tool_catalog"]["tool_23"]["type"] = "Mastoid Retractor"
    trt["tool_catalog"]["tool_23"]["normalized_type"] = "MASTOID_RETRACTOR"
    composite = candidate(
        "i want to adjust the throughput/hr for all production lines to at least 60; "
        "set the tooling picking target for production lines 2 and 4 to retractors; "
        "and adjust the tooling picking order for production lines 1 and 3 to prioritize picking tooling other than forceps."
    )
    composite["sub_requests"] = [
        {
            "request_type": "KPI_UPDATE",
            "target_scope": "ALL_LINES",
            "target_lines": ["line_1", "line_2", "line_3", "line_4"],
            "kpi_updates": {"min_throughput_per_hour": 60},
            "operator_text": "adjust the throughput/hr for all production lines to at least 60",
        },
        {
            "request_type": "TOOLING_POLICY_UPDATE",
            "target_scope": "MULTIPLE_LINES",
            "target_lines": ["line_2", "line_4"],
            "selected_normalized_types": [
                "DOUBLE_ENDED_SURGICAL_RETRACTOR",
                "NERVE_RETRACTOR",
                "MASTOID_RETRACTOR",
            ],
            "operator_text": "set the tooling picking target for production lines 2 and 4 to retractors",
        },
        {
            "request_type": "MANIPULATOR_PRIORITY_UPDATE",
            "target_scope": "MULTIPLE_LINES",
            "target_lines": ["line_1", "line_3"],
            "manipulator_priority": {
                "enabled": True,
                "policy": "EXPLICIT_TYPE_ORDER",
                "prioritize": "NON_MATCHING_TYPES_FIRST",
                "reference_normalized_types": ["FORCEPS", "SURGICAL_FORCEPS", "SPONGE_FORCEPS"],
                "ordered_normalized_types": [],
                "ordered_tool_ids": [],
                "tie_breaker": "FCFS",
            },
            "operator_text": "adjust the tooling picking order for production lines 1 and 3 to prioritize picking tooling other than forceps",
        },
    ]

    patch = normalize_domain_candidate(composite, trt)
    validation_results, validation_reasons = validate_firewall(patch, trt)

    assert patch["affected_lines"] == ["line_1", "line_2", "line_3", "line_4"]
    assert all(validation_results.values()), validation_reasons
    assert len(patch["sub_requests"]) == 3
    kpi_ops = [op for op in patch["operations"] if op["path"].endswith("/kpi/min_throughput_per_hour")]
    assert [op["path"] for op in kpi_ops] == [
        "/lines/line_1/kpi/min_throughput_per_hour",
        "/lines/line_2/kpi/min_throughput_per_hour",
        "/lines/line_3/kpi/min_throughput_per_hour",
        "/lines/line_4/kpi/min_throughput_per_hour",
    ]
    assert all(op["value"] == 60 for op in kpi_ops)

    type_target_ops = [op for op in patch["operations"] if op["path"].endswith("/selected_normalized_types")]
    assert [op["path"] for op in type_target_ops] == [
        "/lines/line_2/selected_normalized_types",
        "/lines/line_4/selected_normalized_types",
    ]
    selected_tool_ops = [op for op in patch["operations"] if op["path"].endswith("/selected_tool_ids")]
    assert [op["path"] for op in selected_tool_ops] == [
        "/lines/line_2/selected_tool_ids",
        "/lines/line_4/selected_tool_ids",
    ]
    assert all(set(op["value"]) == {"tool_20", "tool_21", "tool_23"} for op in selected_tool_ops)

    priority_ops = [op for op in patch["operations"] if op["path"].endswith("/manipulator_priority")]
    assert [op["path"] for op in priority_ops] == [
        "/lines/line_1/manipulator_priority",
        "/lines/line_3/manipulator_priority",
    ]
    for op in priority_ops:
        value = op["value"]
        assert value["policy"] == "EXPLICIT_TYPE_ORDER"
        assert value["prioritize"] == "NON_MATCHING_TYPES_FIRST"
        assert set(value["reference_normalized_types"]) == {"FORCEPS", "SURGICAL_FORCEPS", "SPONGE_FORCEPS"}
        assert "FORCEPS" not in value["ordered_normalized_types"]
        assert "SURGICAL_FORCEPS" not in value["ordered_normalized_types"]
        assert "SPONGE_FORCEPS" not in value["ordered_normalized_types"]


def test_composite_kpi_sub_request_requires_concrete_kpi_updates():
    composite = candidate("adjust the throughput/hr for all production lines to at least 100")
    composite["sub_requests"] = [
        {
            "request_type": "KPI_LIMIT_UPDATE",
            "target_scope": "ALL_LINES",
            "target_lines": [],
            "operator_text": "adjust the throughput/hr for all production lines to at least 100",
        }
    ]

    with pytest.raises(ValueError, match="KPI sub-request is missing concrete kpi_updates"):
        normalize_domain_candidate(composite, current_trt())


def test_compact_complex_time_arrival_candidate_compiles_expected_patch():
    trt = current_trt()
    compact = candidate(
        "okay, two production lines fucked up today. i want to confirm that with only two production lines remaining, "
        "my arrival time can be reduced by about 2.5 seconds, and the time to resolve entanglements can be reduced "
        "by 1.5 seconds. however, to ensure the remaining production lines operate normally, pls stop the robotic arms "
        "immediately upon detecting an anomaly. because of this, pls adjust the recovery time to be 2 second slower, "
        "and set the number of tooling per production line to 5. adjust the throughput/hr for all production lines "
        "to at least 90; set the tooling picking target for production lines 2 to ent tooling set; and adjust the "
        "tooling picking order for production lines 1 to prioritize picking tooling other than knife handle."
    )
    compact.update(
        {
            "request_types": [
                "SIMULATION_CONFIG_UPDATE",
                "KPI_LIMIT_UPDATE",
                "TOOLING_POLICY_UPDATE",
                "MANIPULATOR_PRIORITY_UPDATE",
            ],
            "simulation_config_updates": {
                "num_envs": 2,
                "chosen_intervention_mode": "immediate-stop",
                "travel_time": 2.5,
                "fix_duration": 6.5,
                "resume_delay": 2.5,
                "add_reference_number": 5,
            },
            "kpi_updates": {
                "target_scope": "ALL_LINES",
                "min_throughput_per_hour": 90,
            },
            "tooling_policy_updates": [
                {
                    "target_lines": ["line_2"],
                    "target_set_id": "ENT_SURGICAL_TOOLING_SET",
                }
            ],
            "manipulator_priority_updates": [
                {
                    "target_lines": ["line_1"],
                    "policy": "EXPLICIT_TYPE_ORDER",
                    "prioritize_excluding_normalized_types": ["KNIFE_HANDLE"],
                }
            ],
            "unsupported_terms": [],
            "clarification_questions": [],
        }
    )

    patch = normalize_domain_candidate(compact, trt)
    paths = {operation["path"]: operation["value"] for operation in patch["operations"]}

    assert patch["simulation_config_updates"] == {
        "num_envs": 2,
        "chosen_intervention_mode": "immediate-stop",
        "travel_time": 2.5,
        "fix_duration": 6.5,
        "resume_delay": 2.5,
        "add_reference_number": 5,
    }
    assert paths["/lines/line_1/kpi/min_throughput_per_hour"] == 90
    assert paths["/lines/line_2/kpi/min_throughput_per_hour"] == 90
    assert paths["/lines/line_3/kpi/min_throughput_per_hour"] == 90
    assert paths["/lines/line_4/kpi/min_throughput_per_hour"] == 90
    assert paths["/lines/line_2/target_set_id"] == "ENT_SURGICAL_TOOLING_SET"
    priority = paths["/lines/line_1/manipulator_priority"]
    assert priority["policy"] == "EXPLICIT_TYPE_ORDER"
    assert priority["prioritize"] == "NON_MATCHING_TYPES_FIRST"
    assert priority["reference_normalized_types"] == ["KNIFE_HANDLE"]
    assert patch.get("unsupported_terms") in (None, [])


def test_ambiguous_prioritize_adjustment_requires_clarification():
    with pytest.raises(ValueError, match="Do you mean production-line priority"):
        normalize_domain_candidate(
            candidate("prioritize the adjustment of production lines 1 and 3 operator_id: op_001 reason: milestone 10 test"),
            current_trt(),
        )


def test_tooling_count_request_maps_to_simulation_config_without_operator_arg_name():
    assert parse_tooling_count_request("adjust the number of tooling on the production line so that only 5 remain") == {
        "add_reference_number": 5
    }
    assert parse_tooling_count_request("limit the line to 6 tools") == {"add_reference_number": 6}


def test_precheck_allows_ent_focus_priority_and_clarifies_ambiguous_adjustment():
    trt = current_trt()
    focus = deterministic_intent_precheck(
        "prioritize production lines 1 and 3 to focus on the ENT surgical tooling set operator_id: op_001 reason: milestone 10 test",
        trt,
    )
    ambiguous = deterministic_intent_precheck(
        "prioritize the adjustment of production lines 1 and 3 operator_id: op_001 reason: milestone 10 test",
        trt,
    )

    assert focus["action"] == "PROPOSE_PATCH"
    assert "MANIPULATOR_PRIORITY_UPDATE" in focus["detected_request_types"]
    assert focus["clarification_questions"] == []
    assert ambiguous["action"] == "NEEDS_CLARIFICATION"
    assert ambiguous["clarification_questions"] == [
        "Do you mean production-line priority, or should the robots on lines 1 and 3 pick ENT-required tooling first?"
    ]


def test_ambiguous_all_lines_priority_clarification_preserves_scope():
    trt = current_trt()
    ambiguous = deterministic_intent_precheck(
        "prioritize the adjustment of all production lines to focus on the ENT surgical tooling set",
        trt,
    )

    assert ambiguous["action"] == "NEEDS_CLARIFICATION"
    assert ambiguous["clarification_questions"] == [
        "Do you mean production-line priority, or should the robots on all production lines pick ENT-required tooling first?"
    ]
    assert "lines 1 and 3" not in ambiguous["clarification_questions"][0]


@pytest.mark.parametrize(
    ("intent_text", "line_id", "policy"),
    [
        ("line 4 should pick unwanted tools first operator_id: op_001 reason: test priority", "line_4", "UNWANTED_FIRST"),
        ("line 2 should pick scissors before forceps and knife handles operator_id: op_001 reason: test explicit order", "line_2", "EXPLICIT_TYPE_ORDER"),
        ("line 1 should pick tool_15, then tool_09, then tool_10 operator_id: op_001 reason: test explicit tool priority", "line_1", "EXPLICIT_TOOL_ORDER"),
    ],
)
def test_manipulator_priority_dialogue_variants(intent_text, line_id, policy):
    patch = normalize_domain_candidate(candidate(intent_text), current_trt())

    assert patch["affected_lines"] == [line_id]
    assert patch["operations"] == [
        {
            "op": "add",
            "path": f"/lines/{line_id}/manipulator_priority",
            "value": {
                "policy": policy,
                "ordered_tool_ids": ["tool_15", "tool_09", "tool_10"] if policy == "EXPLICIT_TOOL_ORDER" else [],
                "ordered_normalized_types": ["SCISSORS", "FORCEPS", "KNIFE_HANDLE"] if policy == "EXPLICIT_TYPE_ORDER" else [],
                "tie_breaker": "FCFS",
                "enabled": True,
            },
        }
    ]


def test_missing_line_grasp_order_clarifies_scope():
    with pytest.raises(ValueError, match="target line"):
        normalize_domain_candidate(candidate("pick scissors first"), current_trt())


def test_scenario_spec_includes_manipulator_priority(tmp_path):
    trt = current_trt()
    trt["lines"]["line_1"]["manipulator_priority"] = {
        "policy": "REQUIRED_FIRST",
        "ordered_tool_ids": [],
        "ordered_normalized_types": [],
        "tie_breaker": "FCFS",
        "enabled": True,
    }
    template = {
        "template_id": "test",
        "scene_template": "pick_up_example.py",
        "workspace_contract": {
            "producer_workspace": "governance",
            "consumer_workspace": "isaac_sim",
            "exchange_mode": "file",
            "scenario_specs_dir": "outputs/scenario_specs",
            "run_artifacts_dir": "outputs/run_artifacts",
        },
        "simulation_config": {
            "num_envs": 4,
            "headless": False,
            "layout_source": "auto",
            "episode_success_requires_reset_cycles": 1,
            "add_reference_number": 27,
            "allowed_overlap_ratio": 0.99,
            "reuse_verified_seed": True,
            "chosen_intervention_mode": "continue-until-arrival",
            "travel_time": 5.0,
            "fix_duration": 8.0,
            "resume_delay": 0.5,
        },
        "line_bindings": [
            {"line_id": f"line_{index}", "env_id": index - 1, "enabled": True}
            for index in range(1, 5)
        ],
        "operator_model": {"travel_time": 5.0, "fix_duration": 8.0, "resume_delay": 0.5},
        "abnormal_event_policy": {"entanglement": {"generation_mode": "implicit_runtime_detection", "enabled": True, "allowed_overlap_ratio": 0.25}},
        "assertions": {"use_existing_validation_module": True},
    }
    plan = {
        "plan_id": "rec",
        "trt_id": "trt-demo",
        "trt_version": "v1",
        "affected_lines": ["line_1"],
        "line_decisions": [{"line_id": f"line_{index}", "decision": "NO_CHANGE", "risk_flags": []} for index in range(1, 5)],
    }

    spec = generate_scenario_spec(
        ScenarioGenerationRequest(
            release_id="rel",
            trt_id="trt-demo",
            trt_version="v1",
            reconciliation_plan_id="rec",
            candidate_strategy_id="primary",
            scenario_template_id="test",
            include_waiting_scenarios=False,
            affected_lines=["line_1"],
            line_decisions=plan["line_decisions"],
        ),
        trt=trt,
        plan=plan,
        template=template,
        output_dir=tmp_path,
    )
    line_1 = next(policy for policy in spec["line_policies"] if policy["line_id"] == "line_1")
    line_2 = next(policy for policy in spec["line_policies"] if policy["line_id"] == "line_2")

    assert line_1["manipulator_priority"]["policy"] == "REQUIRED_FIRST"
    assert line_2["manipulator_priority"]["policy"] == "FCFS"
    assert spec["simulation_scope"]["mode"] == "FULL_SYSTEM_DEFAULT"
    assert len(spec["simulation_scope"]["lines"]) == 4


def test_result_reader_imports_priority_summary(tmp_path):
    db_path = tmp_path / "sim.sqlite"
    run_id = "sim-test"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE simulation_runs(run_id TEXT PRIMARY KEY, scenario_spec_id TEXT, scenario_spec_path TEXT, started_at TEXT, completed_at TEXT, status TEXT, error_message TEXT);
            CREATE TABLE line_kpis(run_id TEXT, line_id TEXT, throughput_per_hour REAL, completed_count INTEGER, wanted_completed_count INTEGER, unwanted_completed_count INTEGER, misplaced_count INTEGER, entanglement_count INTEGER, downtime_seconds REAL, cycle_time_seconds REAL, success INTEGER, required_tray_completion_seconds REAL, unwanted_box_completion_seconds REAL, all_sorting_completion_seconds REAL, priority_deviation_count INTEGER, priority_policy TEXT);
            CREATE TABLE tool_events(run_id TEXT, line_id TEXT, tool_id TEXT, tool_type TEXT, env_id INTEGER, tool_number INTEGER, wanted INTEGER, picked INTEGER, placed INTEGER, placement_target TEXT, placement_correct INTEGER, event_time_seconds REAL, actual_pick_index INTEGER, intended_priority_rank INTEGER, priority_policy TEXT);
            CREATE TABLE priority_config(run_id TEXT, scenario_spec_id TEXT, line_id TEXT, env_id INTEGER, priority_policy TEXT, wanted_tool_numbers TEXT, unwanted_tool_numbers TEXT, priority_map_json TEXT);
            CREATE TABLE priority_events(run_id TEXT, line_id TEXT, env_id INTEGER, tool_id TEXT, tool_number INTEGER, intended_rank INTEGER, actual_pick_index INTEGER, priority_policy TEXT, deviation_reason TEXT, event_time_seconds REAL);
            CREATE TABLE container_completion_events(run_id TEXT, line_id TEXT, env_id INTEGER, container_type TEXT, completed_at_seconds REAL, required_count INTEGER, completed_count INTEGER, success INTEGER);
            CREATE TABLE line_completion_kpis(run_id TEXT, line_id TEXT, env_id INTEGER, priority_policy TEXT, required_tray_completion_seconds REAL, unwanted_box_completion_seconds REAL, all_sorting_completion_seconds REAL, priority_deviation_count INTEGER, success INTEGER);
            """
        )
        connection.execute("INSERT INTO simulation_runs VALUES (?, ?, ?, ?, ?, ?, ?)", (run_id, "scn", "spec", "a", "b", "COMPLETED", None))
        connection.execute(
            "INSERT INTO line_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, "line_1", 120, 2, 1, 1, 0, 0, 0, 4, 1, 2.0, 4.0, 4.0, 0, "REQUIRED_FIRST"),
        )
        connection.execute(
            "INSERT INTO priority_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, "line_1", 0, "tool_06", 6, 0, 0, "REQUIRED_FIRST", None, 1.0),
        )
        connection.execute(
            "INSERT INTO priority_config VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, "scn", "line_1", 0, "REQUIRED_FIRST", "[6, 7]", "[1]", "{\"6\": 0, \"1\": 1}"),
        )
        connection.commit()

    artifact = read_simulation_results(db_path, run_id)

    assert artifact["status"] == "COMPLETED"
    assert artifact["priority_summary"]["line_1"]["priority_policy"] == "REQUIRED_FIRST"
    assert artifact["priority_config_count"] == 1
    assert artifact["priority_config"][0]["priority_policy"] == "REQUIRED_FIRST"
    assert artifact["priority_events_count"] == 1


def test_external_isaac_priority_helper_if_available():
    helper_path = Path(
        r"C:\Dev\IsaacSim\_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5\tasks\tools_classification.py"
    )
    if not helper_path.exists():
        pytest.skip("Isaac UR5 helper is not available on this machine.")
    spec = importlib.util.spec_from_file_location("isaac_tools_classification", helper_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    trt = current_trt()
    scenario_spec = {
        "tool_catalog": trt["tool_catalog"],
        "tool_sets": trt["tool_sets"],
        "line_bindings": [{"line_id": "line_1", "env_id": 0}],
        "line_policies": [
            {
                "line_id": "line_1",
                "target_set_id": "ENT_SURGICAL_TOOLING_SET",
                "excluded_tool_ids": [],
                "manipulator_priority": {
                    "policy": "REQUIRED_FIRST",
                    "ordered_tool_ids": [],
                    "ordered_normalized_types": [],
                    "tie_breaker": "FCFS",
                    "enabled": True,
                },
            }
        ],
    }

    priority_by_env = module.build_env_tool_priority(scenario_spec)

    assert priority_by_env[0][6] == 0
    assert priority_by_env[0][1] == 1
