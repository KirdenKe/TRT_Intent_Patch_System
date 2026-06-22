from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from trt_core.evidence_extractor import build_evidence_summary, simulated_deploy
from trt_core.repository import TRTRepository


def _trt() -> dict:
    return {
        "trt_id": "trt-demo",
        "version": "v1",
        "lines": {
            f"line_{index}": {
                "goal": "ROUTINE_CLASSIFICATION",
                "target_set_id": "ENT_SURGICAL_TOOLING_SET",
                "kpi": {
                    "min_throughput_per_hour": 120,
                    "deadline_minutes": None,
                    "max_downtime_seconds": None,
                },
            }
            for index in range(1, 5)
        },
    }


def _state_records() -> list[dict]:
    return [
        {
            "line_id": f"line_{index}",
            "mode": "IDLE",
            "current_task": None,
            "wip_count": 0,
            "checkpoint": "NONE",
            "current_instruments": [],
            "locked_resources": [],
            "selected_tool_ids": ["tool_01"],
            "completed_tool_ids": ["tool_02"],
            "pending_tool_ids": ["tool_03"],
            "entanglement": {"detected": True, "requires_operator": True, "severity": "LOW", "tool_ids": ["tool_04"]},
        }
        for index in range(1, 5)
    ]


def _scenario_spec(repo: TRTRepository, add_reference_number: int = 5) -> dict:
    spec = {
        "scenario_spec_id": "scn_test",
        "trt_id": "trt-demo",
        "trt_version": "v1",
        "simulation_scope": {
            "mode": "FULL_SYSTEM_DEFAULT",
            "lines": ["line_1", "line_2", "line_3", "line_4"],
        },
        "simulation_config": {
            "headless": False,
            "layout_source": "auto",
            "episode_success_requires_reset_cycles": 1,
            "allowed_overlap_ratio": 0.99,
            "chosen_intervention_mode": "continue-until-arrival",
            "travel_time": 5.0,
            "fix_duration": 8.0,
            "resume_delay": 0.5,
            "add_reference_number": add_reference_number,
            "reuse_verified_seed": True,
        },
        "line_policies": [
            {
                "line_id": f"line_{index}",
                "target_set_id": "ENT_SURGICAL_TOOLING_SET",
                "manipulator_priority": {"policy": "FCFS", "enabled": False},
            }
            for index in range(1, 5)
        ],
    }
    path = repo.root / "outputs" / "scenario_specs" / "scn_test.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec), encoding="utf-8")
    return spec


def _result_db(repo: TRTRepository, run_id: str, *, status: str = "COMPLETED", line_4_throughput: float = 130) -> Path:
    path = repo.root / "outputs" / "run_artifacts" / f"{run_id}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE simulation_runs(run_id TEXT PRIMARY KEY, scenario_spec_id TEXT, scenario_spec_path TEXT, started_at TEXT, completed_at TEXT, status TEXT, error_message TEXT);
            CREATE TABLE line_kpis(run_id TEXT, line_id TEXT, throughput_per_hour REAL, completed_count INTEGER, wanted_completed_count INTEGER, unwanted_completed_count INTEGER, misplaced_count INTEGER, entanglement_count INTEGER, downtime_seconds REAL, cycle_time_seconds REAL, success INTEGER, priority_deviation_count INTEGER, priority_policy TEXT);
            CREATE TABLE tool_events(run_id TEXT, line_id TEXT, tool_id TEXT, tool_type TEXT, wanted INTEGER, picked INTEGER, placed INTEGER, placement_target TEXT, placement_correct INTEGER, event_time_seconds REAL);
            """
        )
        connection.execute(
            "INSERT INTO simulation_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, "scn_test", "outputs/scenario_specs/scn_test.json", "a", "b", status, None),
        )
        for index in range(1, 5):
            throughput = line_4_throughput if index == 4 else 130
            connection.execute(
                "INSERT INTO line_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, f"line_{index}", throughput, 10, 5, 5, 0, 0, 0, 1, 1, 0, "FCFS"),
            )
        connection.execute(
            "INSERT INTO tool_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, "line_1", "tool_01", "FORCEPS", 1, 1, 1, "REQUIRED_TRAY", 1, 0),
        )
    return path


def _repo(tmp_path: Path) -> TRTRepository:
    repo = TRTRepository(tmp_path)
    repo.save_trt(_trt())
    repo.save_current_trt_snapshot(_trt())
    repo.save_state_records(_state_records())
    _scenario_spec(repo)
    return repo


def test_evidence_summary_passes_and_recommends_deployment(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _result_db(repo, "sim_pass")

    evidence = build_evidence_summary(repository=repo, run_id="sim_pass", scenario_spec_id="scn_test", trt_id="trt-demo", trt_version="v1")

    assert evidence["status"] == "EVIDENCE_READY"
    assert evidence["evidence_summary"]["overall_result"] == "PASS"
    assert evidence["evidence_summary"]["deployment_recommended"] is True
    assert evidence["evidence_summary"]["next_action"] == "ASK_DEPLOY_APPROVAL"


def test_evidence_summary_blocks_deployment_for_failed_kpi(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _result_db(repo, "sim_fail", line_4_throughput=90)

    evidence = build_evidence_summary(repository=repo, run_id="sim_fail", scenario_spec_id="scn_test", trt_id="trt-demo", trt_version="v1")

    assert evidence["evidence_summary"]["overall_result"] == "FAIL"
    assert evidence["evidence_summary"]["deployment_recommended"] is False
    failed_checks = evidence["failed_checks"]
    assert failed_checks
    assert failed_checks[0]["line_id"] == "line_4"
    assert failed_checks[0]["check_id"] == "throughput.min_per_hour"
    assert failed_checks[0]["technical_check_id"] == "throughput.min_per_hour"
    assert failed_checks[0]["operator_label"] == "Throughput target was missed"
    assert "processed fewer tools per hour" in failed_checks[0]["operator_explanation"]
    assert failed_checks[0]["expected"] == "at least 120 tools/hr"
    assert failed_checks[0]["actual"] == "90 tools/hr"
    assert failed_checks[0]["evidence_source"] == "line_kpis.throughput_per_hour"
    assert failed_checks[0]["deployment_blocking"] is True
    assert evidence["line_results"][-1]["failed_check_ids"] == ["throughput.min_per_hour"]
    assert "line_4" in evidence["errors"][0]


def test_evidence_summary_uses_operator_language_for_priority_and_batch_failures(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec_path = repo.root / "outputs" / "scenario_specs" / "scn_test.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    for policy in spec["line_policies"]:
        policy["manipulator_priority"] = {"policy": "REQUIRED_FIRST", "enabled": True}
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    db_path = _result_db(repo, "sim_priority_fail")
    with sqlite3.connect(db_path) as connection:
        connection.execute("ALTER TABLE line_kpis ADD COLUMN required_tray_completion_seconds REAL")
        connection.execute("ALTER TABLE line_kpis ADD COLUMN unwanted_box_completion_seconds REAL")
        connection.execute("ALTER TABLE line_kpis ADD COLUMN all_sorting_completion_seconds REAL")
        connection.execute("UPDATE line_kpis SET priority_deviation_count = 1, priority_policy = 'REQUIRED_FIRST' WHERE line_id = 'line_1'")
        connection.executescript(
            """
            CREATE TABLE priority_events(run_id TEXT, line_id TEXT, env_id INTEGER, tool_id TEXT, tool_number INTEGER, wanted INTEGER, actual_pick_index INTEGER, intended_priority_rank INTEGER, priority_policy TEXT, deviation_reason TEXT, event_time_seconds REAL);
            CREATE TABLE batch_completion_kpis(run_id TEXT, scenario_spec_id TEXT, line_id TEXT, env_id INTEGER, batch_id TEXT, batch_index INTEGER, table_tool_count INTEGER, picked_count INTEGER, wanted_count INTEGER, unwanted_count INTEGER, batch_started_at_seconds REAL, batch_completed_at_seconds REAL, next_batch_requested_at_seconds REAL, batch_gating_violation INTEGER, success INTEGER);
            """
        )
        connection.execute(
            "INSERT INTO priority_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "sim_priority_fail",
                "line_1",
                0,
                "tool_03",
                3,
                0,
                0,
                1,
                "REQUIRED_FIRST",
                "The first picked tool was unwanted while wanted ENT tools were still available.",
                0.0,
            ),
        )
        connection.execute(
            "INSERT INTO priority_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "sim_priority_fail",
                "line_1",
                0,
                "tool_11",
                11,
                1,
                1,
                0,
                "REQUIRED_FIRST",
                None,
                1.0,
            ),
        )
        connection.execute(
            "INSERT INTO batch_completion_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sim_priority_fail", "scn_test", "line_1", 0, "line_1_batch_0", 0, 6, 2, 4, 2, 0.0, None, 1.0, 1, 0),
        )

    evidence = build_evidence_summary(repository=repo, run_id="sim_priority_fail", scenario_spec_id="scn_test", trt_id="trt-demo", trt_version="v1")

    checks = [check for check in evidence["failed_checks"] if check["line_id"] == "line_1"]
    labels = {check["operator_label"] for check in checks}
    explanations = " ".join(check["operator_explanation"] for check in checks)
    assert "Table was not cleared before the next batch arrived" in labels
    assert "ENT-required tools were not picked first" in labels
    assert "next group of tools before all tools already on the table were picked" in explanations
    assert "non-ENT tooling before finishing the ENT-required tools" in explanations
    assert "batch_gating.table_not_empty_before_next_batch" not in evidence["evidence_summary"]["operator_summary"]
    assert "priority.required_first" not in evidence["evidence_summary"]["operator_summary"]


def test_required_first_passes_when_unwanted_picked_after_batch_wanted_tools(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec_path = repo.root / "outputs" / "scenario_specs" / "scn_test.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    for policy in spec["line_policies"]:
        if policy["line_id"] == "line_3":
            policy["manipulator_priority"] = {"policy": "REQUIRED_FIRST", "enabled": True}
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    db_path = _result_db(repo, "sim_line3_priority_pass")
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE line_kpis SET priority_policy = 'REQUIRED_FIRST' WHERE line_id = 'line_3'")
        connection.executescript(
            """
            CREATE TABLE priority_events(run_id TEXT, line_id TEXT, env_id INTEGER, batch_index INTEGER, tool_id TEXT, tool_number INTEGER, wanted INTEGER, actual_pick_index INTEGER, actual_pick_index_in_batch INTEGER, intended_priority_rank INTEGER, priority_policy TEXT, deviation_reason TEXT, event_time_seconds REAL);
            CREATE TABLE batch_completion_kpis(run_id TEXT, scenario_spec_id TEXT, line_id TEXT, env_id INTEGER, batch_id TEXT, batch_index INTEGER, table_tool_count INTEGER, picked_count INTEGER, wanted_count INTEGER, unwanted_count INTEGER, batch_started_at_seconds REAL, batch_completed_at_seconds REAL, next_batch_requested_at_seconds REAL, batch_gating_violation INTEGER, success INTEGER);
            """
        )
        picks = [
            ("tool_11", 11, 1, 0, 0),
            ("tool_02", 2, 0, 1, 1),
            ("tool_17", 17, 0, 2, 1),
            ("tool_16", 16, 0, 3, 1),
            ("tool_03", 3, 0, 4, 1),
        ]
        for tool_id, tool_number, wanted, pick_index, rank in picks:
            connection.execute(
                "INSERT INTO priority_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "sim_line3_priority_pass",
                    "line_3",
                    2,
                    0,
                    tool_id,
                    tool_number,
                    wanted,
                    pick_index,
                    pick_index,
                    rank,
                    "REQUIRED_FIRST",
                    None,
                    float(pick_index),
                ),
            )
        connection.execute(
            "INSERT INTO batch_completion_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sim_line3_priority_pass", "scn_test", "line_3", 2, "line_3_batch_0", 0, 5, 5, 1, 4, 0.0, 5.0, 6.0, 1, 1),
        )

    evidence = build_evidence_summary(
        repository=repo,
        run_id="sim_line3_priority_pass",
        scenario_spec_id="scn_test",
        trt_id="trt-demo",
        trt_version="v1",
    )

    line_3 = next(row for row in evidence["line_results"] if row["line_id"] == "line_3")
    assert line_3["priority_required_first"]["status"] == "PASS"
    assert line_3["batch_gating"]["status"] == "RECOVERED_WARNING"
    assert line_3["failed_checks"] == []
    assert "line_3" not in evidence["failed_lines"]
    assert "batch_gating.recovered_blocked_next_batch_request" in line_3["warning_check_ids"]


def test_blocked_batch_attempt_does_not_fail_when_batch_later_clears(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    db_path = _result_db(repo, "sim_blocked_attempt_pass")
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE line_kpis SET priority_policy = 'REQUIRED_FIRST' WHERE line_id = 'line_4'")
        connection.executescript(
            """
            CREATE TABLE batch_completion_kpis(run_id TEXT, scenario_spec_id TEXT, line_id TEXT, env_id INTEGER, batch_id TEXT, batch_index INTEGER, table_tool_count INTEGER, picked_count INTEGER, wanted_count INTEGER, unwanted_count INTEGER, batch_started_at_seconds REAL, batch_completed_at_seconds REAL, next_batch_requested_at_seconds REAL, batch_gating_violation INTEGER, success INTEGER);
            CREATE TABLE priority_events(run_id TEXT, line_id TEXT, env_id INTEGER, batch_index INTEGER, tool_id TEXT, tool_number INTEGER, wanted INTEGER, actual_pick_index INTEGER, actual_pick_index_in_batch INTEGER, intended_priority_rank INTEGER, priority_policy TEXT, deviation_reason TEXT, event_time_seconds REAL);
            """
        )
        for tool_id, tool_number, wanted, pick_index, rank in [
            ("tool_24", 24, 1, 0, 0),
            ("tool_12", 12, 1, 1, 0),
            ("tool_14", 14, 1, 2, 0),
            ("tool_02", 2, 0, 3, 1),
            ("tool_05", 5, 0, 4, 1),
        ]:
            connection.execute(
                "INSERT INTO priority_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "sim_blocked_attempt_pass",
                    "line_4",
                    3,
                    0,
                    tool_id,
                    tool_number,
                    wanted,
                    pick_index,
                    pick_index,
                    rank,
                    "REQUIRED_FIRST",
                    None,
                    float(pick_index),
                ),
            )
        connection.execute(
            "INSERT INTO batch_completion_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "sim_blocked_attempt_pass",
                "scn_test",
                "line_4",
                3,
                "line_4_batch_0",
                0,
                5,
                5,
                3,
                2,
                0.0,
                157.2,
                117.9,
                1,
                1,
            ),
        )

    evidence = build_evidence_summary(
        repository=repo,
        run_id="sim_blocked_attempt_pass",
        scenario_spec_id="scn_test",
        trt_id="trt-demo",
        trt_version="v1",
    )

    line_4 = next(row for row in evidence["line_results"] if row["line_id"] == "line_4")
    assert line_4["batch_gating"]["status"] == "RECOVERED_WARNING"
    assert "batch_gating.table_not_empty_before_next_batch" not in line_4["failed_check_ids"]
    assert "line_4" not in evidence["failed_lines"]
    assert "batch_gating.recovered_blocked_next_batch_request" in line_4["warning_check_ids"]


def test_limited_tooling_run_explains_full_environment_kpi_mismatch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec_path = repo.root / "outputs" / "scenario_specs" / "scn_test.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    for policy in spec["line_policies"]:
        policy["manipulator_priority"] = {"policy": "REQUIRED_FIRST", "enabled": True}
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    db_path = _result_db(repo, "sim_limited_scope")
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE simulation_runs SET status = 'FAILED', error_message = 'Run artifact Assert validation failed.'")
        connection.execute("UPDATE line_kpis SET completed_count = 5, success = 0, priority_policy = 'REQUIRED_FIRST'")
        line_counts = {
            "line_1": (3, 2),
            "line_2": (5, 0),
            "line_3": (1, 4),
            "line_4": (3, 2),
        }
        for line_id, (wanted_count, unwanted_count) in line_counts.items():
            connection.execute(
                "UPDATE line_kpis SET wanted_completed_count = ?, unwanted_completed_count = ? WHERE line_id = ?",
                (wanted_count, unwanted_count, line_id),
            )
        connection.executescript(
            """
            CREATE TABLE container_completion_events(run_id TEXT, line_id TEXT, container_type TEXT, required_count INTEGER, completed_count INTEGER, success INTEGER);
            CREATE TABLE priority_events(run_id TEXT, line_id TEXT, env_id INTEGER, batch_index INTEGER, tool_id TEXT, tool_number INTEGER, wanted INTEGER, actual_pick_index INTEGER, actual_pick_index_in_batch INTEGER, intended_priority_rank INTEGER, priority_policy TEXT, deviation_reason TEXT, event_time_seconds REAL);
            CREATE TABLE batch_completion_kpis(run_id TEXT, scenario_spec_id TEXT, line_id TEXT, env_id INTEGER, batch_id TEXT, batch_index INTEGER, table_tool_count INTEGER, picked_count INTEGER, wanted_count INTEGER, unwanted_count INTEGER, batch_started_at_seconds REAL, batch_completed_at_seconds REAL, next_batch_requested_at_seconds REAL, batch_gating_violation INTEGER, success INTEGER);
            """
        )
        full_counts = {
            "line_1": (20, 3, 7, 2),
            "line_2": (20, 5, 7, 0),
            "line_3": (18, 1, 9, 4),
            "line_4": (18, 3, 9, 2),
        }
        for line_id, (required_count, required_completed, unwanted_count, unwanted_completed) in full_counts.items():
            connection.execute(
                "INSERT INTO container_completion_events VALUES (?, ?, ?, ?, ?, ?)",
                ("sim_limited_scope", line_id, "ALL_SORTING", 27, 5, 0),
            )
            connection.execute(
                "INSERT INTO container_completion_events VALUES (?, ?, ?, ?, ?, ?)",
                ("sim_limited_scope", line_id, "REQUIRED_TRAY", required_count, required_completed, 0),
            )
            connection.execute(
                "INSERT INTO container_completion_events VALUES (?, ?, ?, ?, ?, ?)",
                ("sim_limited_scope", line_id, "UNWANTED_BOX", unwanted_count, unwanted_completed, 0),
            )
        picks_by_line = {
            "line_1": [("tool_19", 19, 1), ("tool_14", 14, 1), ("tool_16", 16, 1), ("tool_02", 2, 0), ("tool_01", 1, 0)],
            "line_2": [("tool_15", 15, 1), ("tool_27", 27, 1), ("tool_16", 16, 1), ("tool_09", 9, 1), ("tool_21", 21, 1)],
            "line_3": [("tool_11", 11, 1), ("tool_02", 2, 0), ("tool_17", 17, 0), ("tool_16", 16, 0), ("tool_03", 3, 0)],
            "line_4": [("tool_24", 24, 1), ("tool_12", 12, 1), ("tool_14", 14, 1), ("tool_02", 2, 0), ("tool_05", 5, 0)],
        }
        for env_id, (line_id, picks) in enumerate(picks_by_line.items()):
            for pick_index, (tool_id, tool_number, wanted) in enumerate(picks):
                connection.execute(
                    "INSERT INTO priority_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "sim_limited_scope",
                        line_id,
                        env_id,
                        0 if not (line_id == "line_4" and pick_index == 4) else 1,
                        tool_id,
                        tool_number,
                        wanted,
                        pick_index,
                        pick_index,
                        0 if wanted else 1,
                        "REQUIRED_FIRST",
                        None,
                        float(pick_index),
                    ),
                )
        for env_id, line_id in enumerate(["line_1", "line_2", "line_3"]):
            wanted_count, unwanted_count = line_counts[line_id]
            connection.execute(
                "INSERT INTO batch_completion_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("sim_limited_scope", "scn_test", line_id, env_id, f"{line_id}_batch_0", 0, 5, 5, wanted_count, unwanted_count, 0.0, 5.0, 6.0, 0, 1),
            )
        connection.execute(
            "INSERT INTO batch_completion_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sim_limited_scope", "scn_test", "line_4", 3, "line_4_batch_0", 0, 4, 4, 3, 1, 0.0, 5.0, 4.0, 1, 1),
        )
        connection.execute(
            "INSERT INTO batch_completion_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sim_limited_scope", "scn_test", "line_4", 3, "line_4_batch_1", 1, 1, 1, 0, 1, 5.0, 6.0, 7.0, 0, 1),
        )
        connection.execute(
            "INSERT INTO tool_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sim_limited_scope", "line_4", "tool_14", "ENT", 1, 1, 1, "REQUIRED_TRAY", 0, 2.0),
        )

    evidence = build_evidence_summary(
        repository=repo,
        run_id="sim_limited_scope",
        scenario_spec_id="scn_test",
        trt_id="trt-demo",
        trt_version="v1",
    )

    summary = evidence["evidence_summary"]
    assert "line KPI success flag was false" not in summary["operator_summary"]
    assert summary["simulation_config"] == {
        "simulated_tooling_count": 5,
        "full_environment_tooling_count": 27,
    }
    assert summary["likely_root_cause"]["code"] == "KPI_EXPECTED_FULL_ENVIRONMENT_BUT_SIMULATION_LIMITED"
    assert summary["risk_tier"] == "OPERATOR_ACK_REQUIRED"
    assert summary["deployment_allowed"] is True
    assert summary["deployment_recommended"] is False
    assert summary["requires_operator_acknowledgement"] is True
    assert summary["operator_options"] == ["DEPLOY_WITH_ACK", "DO_NOT_DEPLOY", "REQUEST_REVISION", "RERUN_SIMULATION"]
    assert "KPI_SCOPE_MISMATCH" in summary["acknowledged_risks"]
    assert {check["check_id"] for check in summary["failed_checks"]} == {
        "kpi_scope.full_environment_expected_for_limited_simulation"
    }
    line_2 = next(row for row in summary["line_results"] if row["line_id"] == "line_2")
    assert "sorted all 5 tools shown" in line_2["operator_reason"]
    assert "not correctly placed" not in line_2["operator_reason"]
    line_4 = next(row for row in summary["line_results"] if row["line_id"] == "line_4")
    assert line_4["placement"]["status"] == "WARNING"
    assert line_4["batch_gating"]["status"] == "RECOVERED_WARNING"
    assert "tool_14 was sent to REQUIRED_TRAY" in line_4["operator_reason"]
    assert "blocked" in line_4["operator_reason"]

    result = simulated_deploy(
        repository=repo,
        run_id="sim_limited_scope",
        scenario_spec_id="scn_test",
        trt_id="trt-demo",
        trt_version="v1",
        operator_id="op_001",
        decision="DEPLOY_WITH_ACK",
        acknowledged_risks=summary["acknowledged_risks"],
    )

    assert result["status"] == "DEPLOYED"
    audit = json.loads((tmp_path / result["deployment_audit_path"]).read_text(encoding="utf-8"))
    assert audit["decision"] == "DEPLOY_WITH_ACK"
    assert audit["operator_acknowledgement_required"] is True
    assert audit["operator_acknowledged"] is True
    assert "KPI_SCOPE_MISMATCH" in audit["acknowledged_risks"]


def test_simulated_deployment_updates_state_and_digital_twin_defaults(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _result_db(repo, "sim_deploy")

    result = simulated_deploy(
        repository=repo,
        run_id="sim_deploy",
        scenario_spec_id="scn_test",
        trt_id="trt-demo",
        trt_version="v1",
        operator_id="op_001",
    )

    assert result["status"] == "DEPLOYED"
    records = repo.load_state_records()
    assert all(record["mode"] == "RUNNING" for record in records)
    assert all(record["selected_tool_ids"] == [] for record in records)
    assert all(record["deployment_status"] == "DEPLOYED" for record in records)
    state_payload = json.loads((tmp_path / "data" / "state_records" / "current_state.json").read_text())
    assert state_payload["active_trt_version"] == "v1"
    assert state_payload["deployment_source"] == "DIGITAL_TWIN_EVIDENCE_APPROVED_BY_OPERATOR"
    defaults = json.loads((tmp_path / "data" / "digital_twin" / "default_simulation_config.json").read_text())
    assert defaults["run_id"] == "sim_deploy"
    assert defaults["simulation_config"]["add_reference_number"] == 5


def test_simulated_deployment_rejects_failed_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _result_db(repo, "sim_bad", line_4_throughput=90)

    result = simulated_deploy(
        repository=repo,
        run_id="sim_bad",
        scenario_spec_id="scn_test",
        trt_id="trt-demo",
        trt_version="v1",
        operator_id="op_001",
    )

    assert result["status"] == "REJECTED"
