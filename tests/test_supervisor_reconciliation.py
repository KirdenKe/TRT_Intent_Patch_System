from __future__ import annotations

import json
from copy import deepcopy

from fastapi.testclient import TestClient

import trt_core.api as api
from trt_core.ent_demo import build_current_state, build_trt, state_object_to_records
from trt_core.repository import TRTRepository
from trt_core.supervisor import reconcile_current_trt


def base_line() -> dict:
    return {
        "goal": "ROUTINE_CLASSIFICATION",
        "allowed_instruments": ["SCISSORS"],
        "excluded_instruments": ["CLAMPS"],
        "priority": 3,
        "kpi": {
            "deadline_minutes": 20,
            "max_downtime_seconds": 30,
            "min_throughput_per_hour": 120,
        },
        "abnormal_strategy": "ASK_OPERATOR",
        "state": {
            "mode": "RUNNING",
            "current_task": "sort_set",
            "wip_count": 4,
            "last_exception": None,
        },
    }


def make_trt(version: str, line: dict, line_2: dict | None = None) -> dict:
    return {
        "trt_id": "trt-demo",
        "version": version,
        "lines": {"line_1": line, "line_2": line_2 or base_line()},
    }


def state_record(
    *,
    mode: str,
    wip_count: int,
    current_task: str | None = "ROUTINE_CLASSIFICATION",
    current_instruments: list[str] | None = None,
    checkpoint: str = "NONE",
) -> dict:
    return {
        "line_id": "line_1",
        "mode": mode,
        "current_task": current_task,
        "wip_count": wip_count,
        "current_instruments": current_instruments if current_instruments is not None else ["SCISSORS"],
        "locked_resources": ["robot_arm_1"] if mode in {"RUNNING", "INTERVENTION", "ERROR"} else [],
        "checkpoint": checkpoint,
        "last_exception": "jam_detected" if mode == "ERROR" else None,
        "updated_at_utc": "2026-05-26T00:00:00Z",
    }


def repo_with_versions(tmp_path, previous_line: dict, target_line: dict) -> TRTRepository:
    repo = TRTRepository(tmp_path)
    unchanged_line_2 = base_line()
    repo.save_trt(make_trt("v1", previous_line, unchanged_line_2))
    repo.save_trt(make_trt("v2", target_line, unchanged_line_2))
    return repo


def decision_for(plan: dict, line_id: str = "line_1") -> dict:
    return next(item for item in plan["line_decisions"] if item["line_id"] == line_id)


def changed_goal_line() -> dict:
    line = deepcopy(base_line())
    line["goal"] = "TRAUMA_SET_PRIORITY"
    return line


def priority_only_line() -> dict:
    line = deepcopy(base_line())
    line["priority"] = 5
    return line


def excluded_scissors_line() -> dict:
    line = deepcopy(base_line())
    line["allowed_instruments"] = ["FORCEPS"]
    line["excluded_instruments"] = ["SCISSORS"]
    return line


def priority_and_excluded_scissors_line() -> dict:
    line = priority_only_line()
    line["excluded_instruments"] = ["SCISSORS"]
    return line


def test_idle_to_immediate_switch(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), changed_goal_line())

    plan = reconcile_current_trt([state_record(mode="IDLE", wip_count=0, current_instruments=[])], repo)

    assert decision_for(plan)["decision"] == "IMMEDIATE_SWITCH"
    assert plan["overall_status"] == "READY"


def test_running_with_no_wip_to_immediate_switch(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), changed_goal_line())

    plan = reconcile_current_trt([state_record(mode="RUNNING", wip_count=0)], repo)

    assert decision_for(plan)["decision"] == "IMMEDIATE_SWITCH"


def test_running_with_wip_waits_for_checkpoint(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), changed_goal_line())

    plan = reconcile_current_trt([state_record(mode="RUNNING", wip_count=3)], repo)
    decision = decision_for(plan)

    assert decision["decision"] == "WAIT_FOR_CHECKPOINT"
    assert decision["required_checkpoint"] == "TRAY_COMPLETE"
    assert plan["overall_status"] == "WAITING"


def test_priority_only_change_while_running_is_immediate(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), priority_only_line())

    plan = reconcile_current_trt([state_record(mode="RUNNING", wip_count=5)], repo)

    assert decision_for(plan)["decision"] == "IMMEDIATE_SWITCH"


def test_excluded_instrument_in_wip_waits_for_checkpoint(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), excluded_scissors_line())

    plan = reconcile_current_trt([state_record(mode="RUNNING", wip_count=2, current_instruments=["SCISSORS"])], repo)
    decision = decision_for(plan)

    assert decision["decision"] == "WAIT_FOR_CHECKPOINT"
    assert "excluded_instrument_in_wip" in decision["risk_flags"]


def test_intervention_waits_for_manual_clearance(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), changed_goal_line())

    plan = reconcile_current_trt([state_record(mode="INTERVENTION", wip_count=1, checkpoint="MANUAL_CLEARANCE_REQUIRED")], repo)
    decision = decision_for(plan)

    assert decision["decision"] == "WAIT_FOR_CHECKPOINT"
    assert decision["required_checkpoint"] == "MANUAL_CLEARANCE_REQUIRED"


def test_error_rejects_incompatible(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), changed_goal_line())

    plan = reconcile_current_trt([state_record(mode="ERROR", wip_count=0)], repo)

    assert decision_for(plan)["decision"] == "REJECT_INCOMPATIBLE"
    assert plan["overall_status"] == "REJECTED"


def test_partial_feasible_strategy_degraded_switch(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), priority_and_excluded_scissors_line())

    plan = reconcile_current_trt([state_record(mode="RUNNING", wip_count=2, current_instruments=["SCISSORS"])], repo)
    decision = decision_for(plan)

    assert decision["decision"] == "DEGRADED_SWITCH"
    assert decision["degraded_strategy"] == "APPLY_PRIORITY_ONLY_DELAY_INSTRUMENT_RESTRICTIONS"
    assert plan["overall_status"] == "DEGRADED"


def test_unchanged_line_no_change(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), base_line())

    plan = reconcile_current_trt([state_record(mode="RUNNING", wip_count=2)], repo)

    assert decision_for(plan)["decision"] == "NO_CHANGE"


def test_plan_hashes_retrieval_and_no_trt_mutation(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), changed_goal_line())
    before_versions = [path.name for path in repo.list_trt_versions("trt-demo")]

    plan = reconcile_current_trt([state_record(mode="RUNNING", wip_count=0)], repo)
    after_versions = [path.name for path in repo.list_trt_versions("trt-demo")]
    loaded = repo.load_reconciliation_plan(plan["plan_id"])

    assert plan["source_state_hash"].startswith("sha256:")
    assert plan["source_trt_hash"].startswith("sha256:")
    assert loaded["plan_id"] == plan["plan_id"]
    assert before_versions == after_versions


def test_state_and_reconciliation_api_endpoints(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), changed_goal_line())
    api.repository = repo
    client = TestClient(api.app)
    state_records = [state_record(mode="IDLE", wip_count=0, current_instruments=[])]

    update_response = client.post("/state/update", json={"state_records": state_records})
    current_response = client.get("/state/current")
    reconcile_response = client.post("/supervisor/reconcile", json={"trt_id": "trt-demo", "trt_version": "v2"})
    loaded_response = client.get(f"/reconciliation/{reconcile_response.json()['plan_id']}")

    assert update_response.status_code == 200
    assert current_response.json()["state_records"] == state_records
    assert reconcile_response.status_code == 200
    assert loaded_response.json()["plan_id"] == reconcile_response.json()["plan_id"]


def test_supervisor_reconcile_api_accepts_ent_trt_schema_with_four_lines(tmp_path):
    repo = TRTRepository(tmp_path)
    repo.save_trt(build_trt())
    repo.save_state_records(state_object_to_records(build_current_state()))
    api.repository = repo
    client = TestClient(api.app)

    response = client.post("/supervisor/reconcile", json={"trt_id": "trt-demo", "trt_version": "v1"})
    body = response.json()

    assert response.status_code == 200
    assert body["trt_id"] == "trt-demo"
    assert body["trt_version"] == "v1"
    assert [decision["line_id"] for decision in body["line_decisions"]] == ["line_1", "line_2", "line_3", "line_4"]


def test_supervisor_reconcile_api_accepts_four_line_current_state_object(tmp_path):
    repo = TRTRepository(tmp_path)
    repo.save_trt(build_trt())
    state_path = tmp_path / "data" / "state_records" / "current_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(build_current_state(), indent=2), encoding="utf-8")
    api.repository = repo
    client = TestClient(api.app)

    response = client.post("/supervisor/reconcile", json={"trt_id": "trt-demo", "trt_version": "v1"})

    assert response.status_code == 200
    assert len(response.json()["line_decisions"]) == 4


def test_supervisor_reconcile_allows_empty_selected_and_excluded_tool_ids(tmp_path):
    repo = TRTRepository(tmp_path)
    trt = build_trt()
    for line in trt["lines"].values():
        line["selected_tool_ids"] = []
        line["excluded_tool_ids"] = []
    repo.save_trt(trt)
    state = build_current_state()
    for line_state in state["lines"].values():
        line_state["selected_tool_ids"] = []
        line_state["pending_tool_ids"] = []
        line_state["completed_tool_ids"] = []
    repo.save_state_records(state_object_to_records(state))
    api.repository = repo
    client = TestClient(api.app)

    response = client.post("/supervisor/reconcile", json={"trt_id": "trt-demo", "trt_version": "v1"})

    assert response.status_code == 200


def test_supervisor_reconcile_missing_changed_line_state_returns_422(tmp_path):
    repo = TRTRepository(tmp_path)
    repo.save_trt(build_trt())
    records = [record for record in state_object_to_records(build_current_state()) if record["line_id"] != "line_4"]
    repo.save_state_records(records)
    api.repository = repo
    client = TestClient(api.app)

    response = client.post("/supervisor/reconcile", json={"trt_id": "trt-demo", "trt_version": "v1"})

    assert response.status_code == 422
    assert "Missing runtime state records for changed TRT lines: line_4" in response.json()["detail"]


def test_supervisor_reconcile_legacy_allowed_instruments_fallback_still_works(tmp_path):
    repo = repo_with_versions(tmp_path, base_line(), excluded_scissors_line())

    plan = reconcile_current_trt([state_record(mode="RUNNING", wip_count=2, current_instruments=["SCISSORS"])], repo)

    assert decision_for(plan)["decision"] == "WAIT_FOR_CHECKPOINT"
    assert "excluded_instrument_in_wip" in decision_for(plan)["risk_flags"]
