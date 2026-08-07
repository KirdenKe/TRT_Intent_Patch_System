from __future__ import annotations

import json
from pathlib import Path

import pytest

from scenario_generation.generator import generate_scenario_spec
from trt_core.experiment_evaluation import (
    auto_human_metrics,
    classify_outcome,
    completion_metrics,
    derive_checkpoint_record,
)
from trt_core.repository import TRTRepository
from trt_core.strategy_selection import (
    _candidate_prompt,
    candidate_generation_grammar_schema,
    candidate_generation_schema,
    candidate_measurements,
    generate_candidate_batch,
    locked_line_policy_fields_from_release,
    rank_candidate_runs,
)
from trt_core.time_arrival_state import load_time_arrival_state, save_time_arrival_state
from tools.m12_adjudicate_n8n_results import adjudicate_combined
from tools.m12_record_semantic_review import record_review
import trt_core.api as api


def _priority(policy: str) -> dict:
    return {
        "policy": policy,
        "ordered_tool_ids": [],
        "ordered_normalized_types": [],
        "tie_breaker": "FCFS",
        "enabled": policy != "FCFS",
    }


def _trt() -> dict:
    line = {
        "goal": "ROUTINE_CLASSIFICATION",
        "allowed_instruments": [],
        "excluded_instruments": [],
        "selected_normalized_types": [],
        "selected_tool_ids": [],
        "excluded_tool_ids": [],
        "required_tool_ids": [],
        "target_set_id": "ENT_SURGICAL_TOOLING_SET",
        "tooling_policy": {"required_scope": "SELECTED_TOOLING"},
        "digital_twin": {},
        "priority": 3,
        "manipulator_priority": _priority("FCFS"),
        "kpi": {"min_throughput_per_hour": 60},
        "abnormal_strategy": "STOP_LINE",
    }
    return {
        "trt_id": "trt-demo",
        "version": "v1",
        "lines": {"line_1": line},
        "tool_catalog": {},
        "tool_sets": {},
    }


def _plan() -> dict:
    return {
        "plan_id": "plan_1",
        "trt_id": "trt-demo",
        "trt_version": "v1",
        "overall_status": "READY",
        "affected_lines": ["line_1"],
        "line_decisions": [
            {
                "line_id": "line_1",
                "decision": "IMMEDIATE_SWITCH",
                "required_checkpoint": None,
                "degraded_strategy": None,
                "risk_flags": [],
            }
        ],
    }


def test_candidate_generation_uses_state_and_sends_no_sampling_parameters(tmp_path):
    repository = TRTRepository(tmp_path)
    save_time_arrival_state(
        {"travel_time": 1.0, "fix_duration": 3.0, "resume_delay": 1.0},
        repository=repository,
        source="TEST",
    )
    captured = {}

    def fake_post(url, body, timeout):
        captured.update({"url": url, "body": body, "timeout": timeout})
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "candidates": [
                                    {
                                        "candidate_strategy_id": "candidate_conservative",
                                        "name": "Conservative",
                                        "rationale": "Stop and pick required tools first.",
                                        "line_policy_overrides": {
                                            "line_1": {
                                                "abnormal_strategy": "STOP_LINE",
                                                "manipulator_priority": _priority("REQUIRED_FIRST"),
                                            }
                                        },
                                        "simulation_config_overrides": {},
                                    },
                                    {
                                        "candidate_strategy_id": "candidate_fcfs",
                                        "name": "FCFS",
                                        "rationale": "Keep first-come first-served ordering.",
                                        "line_policy_overrides": {
                                            "line_1": {
                                                "abnormal_strategy": "CONTINUE_FEASIBLE_TASKS",
                                                "manipulator_priority": _priority("FCFS"),
                                            }
                                        },
                                        "simulation_config_overrides": {
                                            "chosen_intervention_mode": "continue-until-arrival"
                                        },
                                    },
                                ]
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    batch = generate_candidate_batch(
        repository=repository,
        released_trt=_trt(),
        reconciliation_plan=_plan(),
        candidate_count=2,
        post_json=fake_post,
    )

    assert batch["candidate_count"] == 2
    assert batch["locked_simulation_config"]["travel_time"] == 1.0
    assert batch["generation_provenance"]["sampling_parameters_sent"] == []
    for field in (
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "presence_penalty",
        "repetition_penalty",
    ):
        assert field not in captured["body"]
    assert "structured_outputs" not in captured["body"]
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "uniqueItems" in json.dumps(candidate_generation_schema())
    assert "uniqueItems" not in json.dumps(candidate_generation_grammar_schema())
    assert repository.load_strategy_batch(batch["strategy_batch_id"])["status"] == "GENERATED"


def test_candidate_prompt_restricts_diversity_when_intervention_mode_is_locked():
    trt = _trt()
    trt["lines"]["line_1"]["abnormal_strategy"] = "CONTINUE_FEASIBLE_TASKS"
    messages = _candidate_prompt(
        released_trt=trt,
        reconciliation_plan=_plan(),
        state_records=[],
        time_arrival_state={
            "travel_time": 1.0,
            "fix_duration": 3.0,
            "resume_delay": 1.0,
            "state_version": 1,
            "updated_at_utc": "2026-08-06T00:00:00Z",
        },
        candidate_count=3,
        locked_simulation_config={"chosen_intervention_mode": "immediate-stop"},
        locked_line_policy_fields={},
        base_simulation_config={"chosen_intervention_mode": "immediate-stop"},
        candidate_line_ids={"line_1"},
    )

    context = json.loads(messages[1]["content"])
    constraints = context["candidate_generation_constraints"]
    assert constraints["chosen_intervention_mode_is_operator_locked"] is True
    assert constraints["required_chosen_intervention_mode"] == "immediate-stop"
    assert constraints["required_effective_abnormal_strategy"] == "STOP_LINE"
    assert constraints["lines_requiring_that_abnormal_strategy"] == ["line_1"]
    assert constraints["permitted_sources_of_candidate_diversity"] == [
        "line_policy_overrides.manipulator_priority"
    ]
    assert constraints["permitted_line_ids"] == ["line_1"]


def test_candidate_prompt_requires_rigid_baseline_before_exploration():
    baseline = {
        "candidate_strategy_id": "operator_faithful_baseline",
        "name": "Operator-faithful baseline",
        "rationale": "Apply only approved changes.",
        "line_policy_overrides": {},
        "simulation_config_overrides": {},
    }
    messages = _candidate_prompt(
        released_trt=_trt(),
        reconciliation_plan=_plan(),
        state_records=[],
        time_arrival_state={
            "travel_time": 1.0,
            "fix_duration": 3.0,
            "resume_delay": 1.0,
            "state_version": 1,
            "updated_at_utc": "2026-08-06T00:00:00Z",
        },
        candidate_count=2,
        locked_simulation_config={},
        locked_line_policy_fields={},
        base_simulation_config={"chosen_intervention_mode": "immediate-stop"},
        operator_faithful_baseline=baseline,
    )

    assert "Return it exactly as supplied as the first candidate" in messages[0]["content"]
    context = json.loads(messages[1]["content"])
    assert context["operator_faithful_baseline"] == baseline
    assert context["exploratory_candidate_count"] == 2


def test_candidate_generation_retries_invalid_batch(monkeypatch, tmp_path):
    repository = TRTRepository(tmp_path)
    monkeypatch.setenv("STRATEGY_CANDIDATE_GENERATION_ATTEMPTS", "2")
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "candidates": [
                                    {
                                        "candidate_strategy_id": "candidate_implicit",
                                        "name": "Implicit baseline",
                                        "rationale": "Leave the baseline implicit.",
                                        "line_policy_overrides": {},
                                        "simulation_config_overrides": {},
                                    },
                                    {
                                        "candidate_strategy_id": "candidate_explicit",
                                        "name": "Explicit baseline",
                                        "rationale": "Restate the same baseline.",
                                        "line_policy_overrides": {
                                            "line_1": {
                                                "abnormal_strategy": "STOP_LINE",
                                                "manipulator_priority": _priority("FCFS"),
                                            }
                                        },
                                        "simulation_config_overrides": {
                                            "chosen_intervention_mode": "immediate-stop"
                                        },
                                    },
                                ]
                            }
                        )
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "candidates": [
                                    {
                                        "candidate_strategy_id": "candidate_stop",
                                        "name": "Stop",
                                        "rationale": "Keep the immediate-stop baseline.",
                                        "line_policy_overrides": {},
                                        "simulation_config_overrides": {},
                                    },
                                    {
                                        "candidate_strategy_id": "candidate_continue",
                                        "name": "Continue",
                                        "rationale": "Continue feasible work until arrival.",
                                        "line_policy_overrides": {
                                            "line_1": {
                                                "abnormal_strategy": "CONTINUE_FEASIBLE_TASKS"
                                            }
                                        },
                                        "simulation_config_overrides": {
                                            "chosen_intervention_mode": "continue-until-arrival"
                                        },
                                    },
                                ]
                            }
                        )
                    }
                }
            ],
            "usage": {},
        },
    ]

    def fake_post(url, body, timeout):
        return responses.pop(0)

    batch = generate_candidate_batch(
        repository=repository,
        released_trt=_trt(),
        reconciliation_plan=_plan(),
        candidate_count=2,
        post_json=fake_post,
    )

    assert batch["generation_provenance"]["attempt_count"] == 2
    assert [item["status"] for item in batch["generation_provenance"]["attempts"]] == [
        "INVALID_OUTPUT",
        "VALID",
    ]


def test_candidate_generation_keeps_operator_faithful_baseline_when_exploration_fails(
    monkeypatch,
    tmp_path,
):
    repository = TRTRepository(tmp_path)
    monkeypatch.setenv("STRATEGY_CANDIDATE_GENERATION_ATTEMPTS", "2")

    def fake_post(url, body, timeout):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "candidates": [
                                    {
                                        "candidate_strategy_id": "invalid_continue",
                                        "name": "Invalid locked change",
                                        "rationale": "Changes a locked operator setting.",
                                        "line_policy_overrides": {
                                            "line_1": {
                                                "abnormal_strategy": "CONTINUE_FEASIBLE_TASKS"
                                            }
                                        },
                                        "simulation_config_overrides": {
                                            "chosen_intervention_mode": "continue-until-arrival"
                                        },
                                    }
                                ]
                            }
                        )
                    }
                }
            ],
            "usage": {},
        }

    batch = generate_candidate_batch(
        repository=repository,
        released_trt=_trt(),
        reconciliation_plan=_plan(),
        candidate_count=3,
        locked_simulation_config={"chosen_intervention_mode": "immediate-stop"},
        locked_line_policy_fields={"line_1": {"abnormal_strategy": "STOP_LINE"}},
        post_json=fake_post,
    )

    assert batch["status"] == "GENERATED_PARTIAL"
    assert batch["requested_candidate_count"] == 3
    assert batch["candidate_count"] == 1
    assert [candidate["candidate_strategy_id"] for candidate in batch["candidates"]] == [
        "operator_faithful_baseline"
    ]
    assert batch["generation_provenance"]["baseline_candidate_source"] == (
        "DETERMINISTIC_OPERATOR_FAITHFUL"
    )
    assert batch["generation_provenance"]["exploratory_generation_status"] == "PARTIAL"


def test_release_operations_lock_operator_approved_line_policy_fields():
    trt = _trt()
    locked = locked_line_policy_fields_from_release(
        {
            "candidate_patch": {
                "operations": [
                    {
                        "op": "replace",
                        "path": "/lines/line_1/manipulator_priority/policy",
                        "value": "REQUIRED_FIRST",
                    },
                    {
                        "op": "replace",
                        "path": "/lines/line_1/kpi/min_throughput_per_hour",
                        "value": 90,
                    },
                ]
            }
        },
        trt,
    )

    assert locked == {
        "line_1": {"manipulator_priority": trt["lines"]["line_1"]["manipulator_priority"]}
    }


def test_candidate_api_requires_released_record_and_passes_locked_fields(
    monkeypatch,
    tmp_path,
):
    repository = TRTRepository(tmp_path)
    monkeypatch.setattr(api, "repository", repository)
    trt = _trt()
    repository.save_trt(trt)
    repository.save_state_records([{"line_id": "line_1", "status": "IDLE"}])
    plan = _plan()
    repository.save_reconciliation_plan(plan)
    release = {
        "release_id": "release_1",
        "trt_id": "trt-demo",
        "trt_version": "v1",
        "status": "RELEASED",
        "candidate_patch": {
            "operations": [
                {
                    "op": "replace",
                    "path": "/lines/line_1/manipulator_priority",
                    "value": _priority("FCFS"),
                }
            ]
        },
    }
    repository.save_release_record(release)
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return {
            "strategy_batch_id": "strategy_batch_api",
            "candidate_count": 2,
            "status": "GENERATED",
            "candidates": [],
        }

    monkeypatch.setattr(api, "generate_candidate_batch", fake_generate)
    result = api.post_strategy_candidates_generate(
        {
            "release_id": "release_1",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "reconciliation_plan_id": "plan_1",
            "candidate_count": 2,
        }
    )

    assert result["status"] == "GENERATED"
    assert captured["locked_line_policy_fields"] == {
        "line_1": {"manipulator_priority": _priority("FCFS")}
    }

    release["status"] = "PENDING"
    repository.save_release_record(release)
    with pytest.raises(Exception, match="requires a RELEASED record"):
        api.post_strategy_candidates_generate(
            {
                "release_id": "release_1",
                "trt_id": "trt-demo",
                "trt_version": "v1",
                "reconciliation_plan_id": "plan_1",
            }
        )


def test_candidate_api_limits_exploration_to_reduced_simulation_scope(monkeypatch, tmp_path):
    repository = TRTRepository(tmp_path)
    monkeypatch.setattr(api, "repository", repository)
    trt = _trt()
    template_line = trt["lines"]["line_1"]
    trt["lines"] = {
        f"line_{index}": {**template_line, "line_id": f"line_{index}"}
        for index in range(1, 5)
    }
    repository.save_trt(trt)
    repository.save_state_records(
        [{"line_id": f"line_{index}", "status": "IDLE"} for index in range(1, 5)]
    )
    plan = _plan()
    plan["affected_lines"] = ["line_1", "line_2", "line_3", "line_4"]
    repository.save_reconciliation_plan(plan)
    repository.save_release_record(
        {
            "release_id": "release_1",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "status": "RELEASED",
            "candidate_patch": {"operations": []},
        }
    )
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return {
            "strategy_batch_id": "strategy_batch_limited",
            "candidate_count": 2,
            "status": "GENERATED",
            "candidates": [],
        }

    monkeypatch.setattr(api, "generate_candidate_batch", fake_generate)
    api.post_strategy_candidates_generate(
        {
            "release_id": "release_1",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "reconciliation_plan_id": "plan_1",
            "affected_lines": ["line_1", "line_2", "line_3", "line_4"],
            "simulation_config_updates": {"num_envs": 2},
        }
    )

    assert captured["simulation_line_ids"] == ["line_1", "line_2"]


def test_candidate_generation_failure_becomes_terminal_batch_without_simulation(monkeypatch, tmp_path):
    repository = TRTRepository(tmp_path)
    monkeypatch.setattr(api, "repository", repository)
    trt = _trt()
    repository.save_trt(trt)
    repository.save_state_records([{"line_id": "line_1", "status": "IDLE"}])
    repository.save_reconciliation_plan(_plan())
    repository.save_release_record(
        {
            "release_id": "release_failed_generation",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "status": "RELEASED",
            "candidate_patch": {"operations": []},
        }
    )

    def fail_generation(**kwargs):
        raise ValueError("model grammar rejected candidate output")

    monkeypatch.setattr(api, "generate_candidate_batch", fail_generation)
    result = api.post_strategy_candidates_generate(
        {
            "release_id": "release_failed_generation",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "reconciliation_plan_id": "plan_1",
            "candidate_count": 3,
        }
    )

    assert result["status"] == "GENERATION_FAILED"
    assert result["candidate_runs"] == []
    assert result["selection"]["status"] == "GENERATION_FAILED"
    assert result["selection"]["operator_refinement_required"] is False
    assert result["selection"]["operator_intent_fault_detected"] is False
    assert result["selection"]["failure_classification"] == "SYSTEM_GENERATION_ERROR"
    assert result["selection"]["refinement_suggestions"] == []
    assert "model grammar rejected" in result["generation_error"]
    assert api.post_strategy_batch_run(result["strategy_batch_id"])["status"] == "GENERATION_FAILED"


def test_time_arrival_semantic_retry_accepts_gemma_regeneration(monkeypatch, tmp_path):
    repository = TRTRepository(tmp_path)
    monkeypatch.setattr(api, "repository", repository)
    repository.save_trt(_trt())
    save_time_arrival_state(
        {"travel_time": 1.0, "fix_duration": 3.0, "resume_delay": 1.0},
        repository=repository,
        source="TEST",
    )
    candidate = {
        "patch_id": "patch_retry",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "reason": "test",
        "intent_text": (
            "reduce the current arrival time by 0.5 seconds, reduce the current entanglement "
            "fix time by 1 second, and make the current recovery delay 1 second slower"
        ),
        "simulation_config_updates": {
            "travel_time": 0.0,
            "fix_duration": 2.0,
            "resume_delay": 2.0,
        },
    }

    def fake_post(url, body, timeout_seconds):
        assert "0.5" not in body["messages"][0]["content"]
        assert "including decimal fractions" in body["messages"][0]["content"]
        assert "operator arrival time maps to travel_time" in body["messages"][0]["content"]
        context = json.loads(body["messages"][1]["content"])
        assert context["recorded_time_arrival_state"] == {
            "travel_time": 1.0,
            "fix_duration": 3.0,
            "resume_delay": 1.0,
        }
        assert body["max_tokens"] == 6000
        assert "structured_outputs" not in body
        assert "chat_template_kwargs" not in body
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "time_arrival_derivations": [
                                    {
                                        "field": "travel_time",
                                        "baseline_value": 1.0,
                                        "operation": "SUBTRACT",
                                        "requested_delta_seconds": 0.5,
                                        "result": 0.5,
                                    },
                                    {
                                        "field": "fix_duration",
                                        "baseline_value": 3.0,
                                        "operation": "SUBTRACT",
                                        "requested_delta_seconds": 1.0,
                                        "result": 2.0,
                                    },
                                    {
                                        "field": "resume_delay",
                                        "baseline_value": 1.0,
                                        "operation": "ADD",
                                        "requested_delta_seconds": 1.0,
                                        "result": 2.0,
                                    },
                                ],
                                "simulation_config_updates": {
                                    "travel_time": 0.5,
                                    "fix_duration": 2.0,
                                    "resume_delay": 2.0,
                                    "allowed_overlap_ratio": 0.5,
                                },
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }

    monkeypatch.setattr(api, "_post_json", fake_post)
    regenerated, attempts = api._regenerate_time_arrival_candidate(
        candidate,
        _trt(),
        ["Gemma derived an inconsistent travel_time value"],
    )

    assert regenerated["simulation_config_updates"]["travel_time"] == 0.5
    assert "allowed_overlap_ratio" not in regenerated["simulation_config_updates"]
    assert attempts[0]["status"] == "VALID"
    assert attempts[0]["model_time_arrival_derivations"][0]["result"] == 0.5


def test_dialogue_routing_preserves_task_request_when_time_derivation_is_invalid(
    monkeypatch,
    tmp_path,
):
    repository = TRTRepository(tmp_path)
    monkeypatch.setattr(api, "repository", repository)
    repository.save_trt(_trt())
    save_time_arrival_state(
        {"travel_time": 1.0, "fix_duration": 3.0, "resume_delay": 1.0},
        repository=repository,
        source="TEST",
    )
    intent_text = (
        "with two production lines remaining, reduce the current arrival time by 0.5 seconds, "
        "reduce the current entanglement fix time by 1 second, make the current recovery delay "
        "1 second slower, and simulate 4 tooling per line"
    )

    def fake_post(url, body, timeout_seconds):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "dialogue_state": "UNKNOWN",
                                "turn_type": "TASK_REQUEST",
                                "operator_message": "I need operator ID and reason before review.",
                                "normalized_request": {
                                    "operator_id": None,
                                    "reason": None,
                                    "intent_text": intent_text,
                                    "target_scope": "MULTIPLE_LINES",
                                    "target_lines": [],
                                    "target_set_id": None,
                                    "request_types": ["SIMULATION_CONFIG_UPDATE"],
                                    "kpi_updates": None,
                                    "manipulator_priority": None,
                                    "simulation_config_updates": {
                                        "num_envs": 2,
                                        "travel_time": 0.0,
                                        "fix_duration": 2.0,
                                        "resume_delay": 2.0,
                                        "add_reference_number": 4,
                                    },
                                    "dry_run_only": False,
                                    "deployment_allowed_after_success": None,
                                    "failure_action_hint": None,
                                },
                                "action": "NEEDS_CLARIFICATION",
                                "query_targets": [],
                                "line_ids": [],
                                "scenario_spec_id": None,
                                "run_id": None,
                                "missing_or_unclear_items": ["operator_id", "reason"],
                                "approval_decision": None,
                                "deployment_decision": None,
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(api, "_post_json", fake_post)
    result = api.post_chat_dialogue_decision(
        {
            "session_id": "routing_test",
            "latest_user_message": intent_text,
            "raw_chat_input": intent_text,
        }
    )

    assert result["turn_type"] == "TASK_REQUEST"
    assert result["dialogue_state"] == "NEEDS_CLARIFICATION"
    assert result["status"] != "UNKNOWN"
    assert result["llm_decision_raw"]["semantic_validation_status"] == "INVALID_DIAGNOSTIC"
    assert result["llm_decision_raw"]["llm_semantic_validation_attempts"][0][
        "classification_preserved"
    ] is True
    assert result["llm_decision_raw"]["routing_repairs"][0]["code"] == (
        "TASK_CLASSIFICATION_PRESERVED"
    )


def test_intent_normalize_recovers_explicit_absolute_values_without_model_retry(
    monkeypatch,
    tmp_path,
):
    repository = TRTRepository(tmp_path)
    monkeypatch.setattr(api, "repository", repository)
    repository.save_trt(_trt())
    save_time_arrival_state(
        {"travel_time": 1.0, "fix_duration": 3.0, "resume_delay": 1.0},
        repository=repository,
        source="TEST",
    )
    candidate = {
        "patch_id": "patch_explicit_recovery",
        "trt_id": "trt-demo",
        "base_version": "v1",
        "operator_id": "op_001",
        "reason": "test",
        "intent_text": (
            "my arrival time is 4 seconds, the time required to resolve the tangling issue "
            "is 5 seconds, and the recovery time is 0.5 seconds. continue until i arrive "
            "at the production line. the allow overlap ratio is 0.9"
        ),
        "request_types": ["SIMULATION_CONFIG_UPDATE"],
        "line_id": None,
        "target_scope": "ALL_LINES",
        "target_lines": [],
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
        "kpi_updates": {},
        "tooling_policy": None,
        "abnormal_strategy": None,
        "clarification_questions": [],
        "unsupported_terms": [],
        "detected_request_types": ["SIMULATION_CONFIG_UPDATE"],
        "simulation_config_updates": {
            "travel_time": 3.0,
            "fix_duration": 2.0,
            "resume_delay": 1.5,
            "chosen_intervention_mode": "CONTINUE_UNTIL_ARRIVAL",
        },
        "status": "REVIEWED",
    }

    monkeypatch.setattr(
        api,
        "_post_json",
        lambda *args, **kwargs: pytest.fail("Explicit-value recovery must not call the model"),
    )
    result = api.post_intent_normalize(candidate)

    assert result["intent_patch"]["simulation_config_updates"] == {
        "travel_time": 4.0,
        "fix_duration": 5.0,
        "resume_delay": 0.5,
        "chosen_intervention_mode": "continue-until-arrival",
        "allowed_overlap_ratio": 0.9,
    }
    assert result["llm_semantic_regeneration"][0]["method"] == (
        "DETERMINISTIC_EXPLICIT_VALUE_RECOVERY"
    )


def test_scenario_candidate_override_is_applied_without_changing_kpi(fixture_loader):
    trt = _trt()
    plan = _plan()
    plan["release_id"] = "release_1"
    spec = generate_scenario_spec(
        released_trt={**trt, "release_id": "release_1"},
        state_records=[{"line_id": "line_1"}],
        reconciliation_plan=plan,
        scenario_template_id="ur5_pick_place_minimal",
        candidate_strategy_id="candidate_required_first",
        strategy_batch_id="strategy_batch_1",
        template_registry=fixture_loader("scenario_templates.json"),
        required_line_ids=["line_1"],
        line_policy_overrides={
            "line_1": {
                "abnormal_strategy": "CONTINUE_FEASIBLE_TASKS",
                "manipulator_priority": _priority("REQUIRED_FIRST"),
            }
        },
    )

    policy = spec["line_policies"][0]
    assert policy["manipulator_priority"]["policy"] == "REQUIRED_FIRST"
    assert policy["abnormal_strategy"] == "CONTINUE_FEASIBLE_TASKS"
    assert policy["kpi"]["min_throughput_per_hour"] == 60
    assert spec["governance_metadata"]["strategy_batch_id"] == "strategy_batch_1"


def test_candidate_measurements_require_explicit_placement_and_reset_evidence():
    run_artifact = {
        "status": "COMPLETED",
        "run": {"reset_cycles_requested": 1, "reset_cycles_completed": 1},
        "tool_events": [
            {"placed": 1, "placement_target": "REQUIRED_TRAY", "placement_correct": 1},
            {"placed": 1, "placement_target": "UNWANTED_BOX", "placement_correct": 0},
        ],
        "line_kpis": [
            {
                "line_id": "line_1",
                "throughput_per_hour": 90,
                "priority_deviation_count": 0,
                "batch_gating_violation_count": 0,
                "all_sorting_time_seconds": 30,
            }
        ],
    }
    scenario_spec = {
        "line_policies": [
            {"line_id": "line_1", "kpi": {"min_throughput_per_hour": 60}}
        ]
    }

    result = candidate_measurements(
        run_artifact=run_artifact,
        scenario_spec=scenario_spec,
        evidence_summary={"deployment_allowed": True},
    )

    assert result["R_storage"] == 0.5
    assert result["R_reset"] == 1.0
    assert result["throughput_attainment"] == 1.5
    assert result["eligible"] is False
    assert "PLACEMENT_VERIFICATION_FAILED" in result["blocking_reasons"]

    incomplete = candidate_measurements(
        run_artifact={"status": "COMPLETED", "line_kpis": run_artifact["line_kpis"]},
        scenario_spec=scenario_spec,
        evidence_summary={"deployment_allowed": True},
    )
    assert incomplete["eligible"] is False
    assert "PLACEMENT_EVIDENCE_MISSING" in incomplete["blocking_reasons"]
    assert "RESET_EVIDENCE_MISSING" not in incomplete["blocking_reasons"]
    assert "RESET_EVIDENCE_MISSING" in incomplete["diagnostic_warnings"]
    assert incomplete["R_reset_is_mandatory_constraint"] is False


def test_missing_reset_evidence_is_diagnostic_and_does_not_block_candidate():
    result = candidate_measurements(
        run_artifact={
            "status": "COMPLETED",
            "tool_storage_records": [
                {
                    "actual_target": "required_tray",
                    "verification_passed": True,
                    "placement_correct": True,
                }
            ],
            "line_kpis": [
                {
                    "line_id": "line_1",
                    "throughput_per_hour": 90.0,
                    "priority_deviation_count": 0,
                    "batch_gating_violation_count": 0,
                }
            ],
        },
        scenario_spec={
            "line_policies": [
                {"line_id": "line_1", "kpi": {"min_throughput_per_hour": 80.0}}
            ]
        },
        evidence_summary={"deployment_allowed": True},
    )

    assert result["R_reset"] is None
    assert result["eligible"] is True
    assert result["blocking_reasons"] == []
    assert result["diagnostic_warnings"] == ["RESET_EVIDENCE_MISSING"]
    assert result["data_quality_status"] == "DATA_INCOMPLETE"


def test_priority_deviation_does_not_block_when_evidence_marks_policy_compliant():
    result = candidate_measurements(
        run_artifact={
            "status": "COMPLETED",
            "tool_storage_records": [
                {
                    "actual_target": "required_tray",
                    "verification_passed": True,
                    "placement_correct": True,
                }
            ],
            "line_kpis": [
                {
                    "line_id": "line_1",
                    "throughput_per_hour": 90.0,
                    "priority_deviation_count": 3,
                    "batch_gating_violation_count": 0,
                }
            ],
        },
        scenario_spec={
            "line_policies": [
                {"line_id": "line_1", "kpi": {"min_throughput_per_hour": 85.0}}
            ]
        },
        evidence_summary={
            "deployment_allowed": True,
            "kpi_table": [
                {
                    "line_id": "line_1",
                    "priority_pass": True,
                    "batch_gating": {"status": "PASS"},
                }
            ],
        },
    )

    assert result["eligible"] is True
    assert "PRIORITY_COMPLIANCE_FAILED" not in result["blocking_reasons"]
    assert result["priority_compliance_source"] == "EVIDENCE_SUMMARY"


def test_ranking_excludes_ineligible_candidate_and_selects_highest_throughput():
    common = {
        "eligible": True,
        "R_storage": 1.0,
        "R_reset": 1.0,
        "throughput_attainment": 1.0,
        "priority_deviation_count": 0,
        "batch_gating_violation_count": 0,
        "data_quality_status": "OK",
    }
    selection = rank_candidate_runs(
        [
            {
                "candidate_strategy_id": "candidate_slow",
                "scenario_spec_id": "scn_slow",
                "run_id": "run_slow",
                "measurements": {**common, "throughput_attainment": 1.25, "strategy_simulation_seconds": 50.0},
            },
            {
                "candidate_strategy_id": "candidate_fast",
                "scenario_spec_id": "scn_fast",
                "run_id": "run_fast",
                "measurements": {**common, "throughput_attainment": 1.10, "strategy_simulation_seconds": 30.0},
            },
            {
                "candidate_strategy_id": "candidate_missing",
                "measurements": {
                    "eligible": False,
                    "blocking_reasons": ["PLACEMENT_EVIDENCE_MISSING"],
                },
            },
        ]
    )

    assert selection["status"] == "SELECTED"
    assert selection["selected_candidate_strategy_id"] == "candidate_slow"
    assert selection["selected_run_id"] == "run_slow"
    missing = next(
        row
        for row in selection["ranked_candidates"]
        if row["candidate_strategy_id"] == "candidate_missing"
    )
    assert missing["objective_score"] is None
    assert missing["rank"] is None


def test_candidate_is_blocked_when_any_line_misses_its_throughput_target():
    result = candidate_measurements(
        run_artifact={
            "status": "COMPLETED",
            "run": {"reset_cycles_requested": 1, "reset_cycles_completed": 1},
            "tool_events": [
                {"placed": 1, "placement_target": "REQUIRED_TRAY", "placement_correct": 1},
            ],
            "line_kpis": [
                {
                    "line_id": "line_1",
                    "throughput_per_hour": 180,
                    "priority_deviation_count": 0,
                    "batch_gating_violation_count": 0,
                },
                {
                    "line_id": "line_2",
                    "throughput_per_hour": 45,
                    "priority_deviation_count": 0,
                    "batch_gating_violation_count": 0,
                },
            ],
        },
        scenario_spec={
            "line_policies": [
                {"line_id": "line_1", "kpi": {"min_throughput_per_hour": 90}},
                {"line_id": "line_2", "kpi": {"min_throughput_per_hour": 90}},
            ]
        },
        evidence_summary={"deployment_allowed": True},
    )

    assert result["throughput_attainment"] == 1.25
    assert result["throughput_attainment_by_line"] == {"line_1": 2.0, "line_2": 0.5}
    assert result["throughput_below_target_lines"] == ["line_2"]
    assert result["each_line_throughput_target_met"] is False
    assert result["eligible"] is False
    assert "LINE_THROUGHPUT_TARGET_NOT_MET" in result["blocking_reasons"]


def test_candidate_is_incomplete_when_a_target_line_has_no_throughput_row():
    result = candidate_measurements(
        run_artifact={
            "status": "COMPLETED",
            "run": {"reset_cycles_requested": 1, "reset_cycles_completed": 1},
            "tool_events": [{"placed": 1, "placement_correct": 1}],
            "line_kpis": [
                {
                    "line_id": "line_1",
                    "throughput_per_hour": 100,
                    "priority_deviation_count": 0,
                    "batch_gating_violation_count": 0,
                }
            ],
        },
        scenario_spec={
            "line_policies": [
                {"line_id": "line_1", "kpi": {"min_throughput_per_hour": 90}},
                {"line_id": "line_2", "kpi": {"min_throughput_per_hour": 90}},
            ]
        },
        evidence_summary={"deployment_allowed": True},
    )

    assert result["throughput_missing_lines"] == ["line_2"]
    assert result["eligible"] is False
    assert result["data_quality_status"] == "DATA_INCOMPLETE"
    assert "LINE_THROUGHPUT_EVIDENCE_MISSING" in result["blocking_reasons"]


def test_no_eligible_candidate_requests_operator_refinement_without_regeneration():
    selection = rank_candidate_runs(
        [
            {
                "candidate_strategy_id": "candidate_invalid",
                "status": "EVALUATED",
                "measurements": {
                    "eligible": False,
                    "blocking_reasons": ["PRIORITY_COMPLIANCE_FAILED"],
                },
            }
        ]
    )

    assert selection["status"] == "NO_ELIGIBLE_STRATEGY"
    assert selection["operator_refinement_required"] is True
    assert selection["post_simulation_regeneration_performed"] is False
    assert any("tooling order" in item for item in selection["refinement_suggestions"])


def test_incomplete_candidate_execution_does_not_blame_operator_intent():
    selection = rank_candidate_runs(
        [
            {
                "candidate_strategy_id": "candidate_system_error",
                "status": "SYSTEM_ERROR",
                "measurements": {
                    "eligible": False,
                    "blocking_reasons": ["SYSTEM_ERROR"],
                },
            }
        ]
    )

    assert selection["status"] == "NO_ELIGIBLE_STRATEGY"
    assert selection["operator_refinement_required"] is False
    assert selection["all_candidates_conclusively_evaluated"] is False
    assert selection["failure_classification"] == "SYSTEM_OR_SIMULATION_INCOMPLETE"
    assert selection["refinement_suggestions"] == []
    assert selection["recovery_actions"]


def test_candidate_schema_accepts_safe_opaque_identifier_prefixes():
    schema = candidate_generation_schema()
    id_schema = schema["properties"]["candidates"]["items"]["properties"]["candidate_strategy_id"]
    assert id_schema["pattern"] == "^[A-Za-z][A-Za-z0-9_-]{0,119}$"


def test_time_arrival_state_initializes_from_deployed_defaults(tmp_path):
    repository = TRTRepository(tmp_path)
    defaults = tmp_path / "data" / "digital_twin" / "default_simulation_config.json"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(
        json.dumps(
            {
                "simulation_config": {
                    "travel_time": 1.5,
                    "fix_duration": 2.5,
                    "resume_delay": 0.75,
                }
            }
        ),
        encoding="utf-8",
    )

    state = load_time_arrival_state(repository)

    assert state["travel_time"] == 1.5
    assert state["fix_duration"] == 2.5
    assert state["resume_delay"] == 0.75
    assert state["source"] == "DEPLOYED_SIMULATION_DEFAULTS"


def test_outcome_and_agreement_metrics_keep_assisted_success_separate():
    assert (
        classify_outcome(
            {cp: True for cp in ("CP0", "CP1", "CP2", "CP3", "CP4", "CP5")},
            manual_correction_used=True,
            operator_accepted=True,
        )
        == "MANUALLY_ASSISTED_SUCCESS"
    )
    rows = [
        {
            "test_id": "a",
            "outcome_class": "AUTONOMOUS_SUCCESS",
            "manual_intervention_required": False,
            "automated_result": "PASS",
            "manual_result": "PASS",
        },
        {
            "test_id": "b",
            "outcome_class": "MANUALLY_ASSISTED_SUCCESS",
            "manual_intervention_required": True,
            "automated_result": "FAIL",
            "manual_result": "PASS",
        },
    ]
    completion = completion_metrics(rows)
    agreement = auto_human_metrics(rows)
    assert completion["autonomous_success_rate"] == 0.5
    assert completion["assisted_completion_rate"] == 1.0
    assert completion["overall_completion_rate"] == 1.0
    assert agreement["auto_human_agreement_rate"] == 0.5
    assert agreement["automated_fail_manual_pass"] == ["b"]


def test_checkpoint_projection_does_not_invent_manual_review():
    result = derive_checkpoint_record(
        suite="TC1",
        prompt="set line 1 throughput to 90",
        expected_status="REVIEWED",
        should_launch_isaac=True,
        scenario_spec_id="scn_test",
        run_artifact_exists=True,
        packet_score={
            "status": "PASS",
            "failure_stage": "completed",
            "failure_cause": "Packet expected fields matched.",
            "checks": {
                "scenario_spec_schema_pass": True,
                "target_line_match": True,
                "kpi_update_match": True,
                "simulation_config_match": True,
            },
        },
    )

    assert result["automated_result"] == "PASS"
    assert result["CP0"] is True
    assert result["CP4"] is True
    assert result["CP6"] is None
    assert result["manual_result"] is None
    assert result["human_reviewed"] is False
    assert result["outcome_class"] == "EVALUATION_INCOMPLETE"


def test_secondary_assessment_and_semantic_review_remain_separate(tmp_path):
    combined = {
        "test_id": "TC2-test",
        "session_id": "session-test",
        "row": {
            "test_id": "TC2-test",
            "suite": "TC2",
            "paste_into_n8n": "show current KPI targets",
            "required_tools": json.dumps(["load_current_trt"]),
        },
        "turns": [{"text": "Minimum throughput source: current TRT"}],
        "packet_score": {
            "status": "INCONCLUSIVE",
            "failure_stage": "tool_orchestration",
            "failure_cause": "No trace.",
            "checks": {},
        },
    }
    combined_path = tmp_path / "combined.json"
    combined_path.write_text(json.dumps(combined), encoding="utf-8")

    secondary = adjudicate_combined(combined)
    review = record_review(
        combined_path,
        result="PASS",
        reason="The response answered the KPI query from the current TRT.",
        reviewer_type="CODEX_SEMANTIC_REVIEW",
        output=tmp_path / "reviews.jsonl",
    )

    assert secondary["secondary_automated_status"] == "PASS"
    assert secondary["human_reviewed"] is False
    assert review["review_result"] == "PASS"
    assert review["reviewer_type"] == "CODEX_SEMANTIC_REVIEW"
    assert review["human_reviewed"] is False
    assert review["operator_cp6_result"] is None


def test_strategy_api_routes_and_n8n_sequential_workflow_are_registered():
    paths = api.app.openapi()["paths"]
    assert "/strategy/candidates/generate" in paths
    assert "/strategy/batches/{strategy_batch_id}/run" in paths
    assert "/strategy/batches/{strategy_batch_id}" in paths
    workflow_path = (
        Path(__file__).resolve().parents[1]
        / "n8n_workflows"
        / "generate_scenario_spec.workflow.json"
    )
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assert (
        nodes["Generate Candidate Strategy Batch"]["parameters"]["url"]
        == "http://trt-api:8000/strategy/candidates/generate"
    )
    assert "strategy/batches" in nodes["Start Sequential Candidate Simulations"]["parameters"]["url"]
    assert "strategy/batches" in nodes["Poll Candidate Strategy Batch"]["parameters"]["url"]
    assert "temperature" not in workflow_path.read_text(encoding="utf-8")
    priority_enum = (
        candidate_generation_schema()["properties"]["candidates"]["items"]["properties"]
        ["line_policy_overrides"]["additionalProperties"]["properties"]
        ["manipulator_priority"]["properties"]["policy"]["enum"]
    )
    assert priority_enum == [
        "FCFS",
        "REQUIRED_FIRST",
        "UNWANTED_FIRST",
        "EXPLICIT_TOOL_ORDER",
        "EXPLICIT_TYPE_ORDER",
    ]
    for path in workflow_path.parent.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        for field in (
            "temperature:",
            "top_p:",
            "top_k:",
            "min_p:",
            "presence_penalty:",
            "repetition_penalty:",
        ):
            assert field not in text


def test_strategy_batch_worker_processes_candidates_in_order(monkeypatch, tmp_path):
    repository = TRTRepository(tmp_path)
    monkeypatch.setattr(api, "repository", repository)
    call_order = []
    batch = {
        "strategy_batch_id": "strategy_batch_test",
        "status": "QUEUED",
        "trt_id": "trt-demo",
        "trt_version": "v1",
        "reconciliation_plan_id": "plan_1",
        "candidate_count": 2,
        "candidates": [
            {
                "candidate_strategy_id": "candidate_first",
                "line_policy_overrides": {},
                "simulation_config_overrides": {},
            },
            {
                "candidate_strategy_id": "candidate_second",
                "line_policy_overrides": {},
                "simulation_config_overrides": {},
            },
        ],
        "locked_simulation_config": {
            "travel_time": 1.0,
            "fix_duration": 3.0,
            "resume_delay": 1.0,
        },
        "generation_provenance": {
            "started_at_utc": "2026-01-01T00:00:00Z",
            "completed_at_utc": "2026-01-01T00:00:01Z",
        },
        "execution_request": {
            "release_id": "release_1",
            "trt_id": "trt-demo",
            "trt_version": "v1",
            "reconciliation_plan_id": "plan_1",
        },
        "candidate_runs": [],
        "selection": None,
        "queued_at_utc": "2026-01-01T00:00:01Z",
        "created_at_utc": "2026-01-01T00:00:00Z",
        "updated_at_utc": "2026-01-01T00:00:01Z",
    }
    repository.save_strategy_batch(batch)

    def fake_scenario(payload):
        candidate_id = payload["candidate_strategy_id"]
        call_order.append(("scenario", candidate_id))
        return {
            "status": "GENERATED",
            "scenario_spec_id": f"scn_{candidate_id}",
            "scenario_spec_path": f"outputs/scenario_specs/scn_{candidate_id}.json",
            "scenario_spec": {
                "line_policies": [
                    {"line_id": "line_1", "kpi": {"min_throughput_per_hour": 60}}
                ]
            },
        }

    def fake_simulation(payload):
        candidate_id = payload["scenario_spec_id"].removeprefix("scn_candidate_")
        call_order.append(("simulation", f"candidate_{candidate_id}"))
        return {
            "status": "COMPLETED",
            "run_id": f"run_{candidate_id}",
            "output_db_path": f"outputs/run_artifacts/run_{candidate_id}.sqlite",
            "run_artifact": {
                "status": "COMPLETED",
                "run": {"reset_cycles_requested": 1, "reset_cycles_completed": 1},
                "tool_events": [
                    {
                        "placed": 1,
                        "placement_target": "REQUIRED_TRAY",
                        "placement_correct": 1,
                    }
                ],
                "line_kpis": [
                    {
                        "line_id": "line_1",
                        "throughput_per_hour": 60,
                        "priority_deviation_count": 0,
                        "batch_gating_violation_count": 0,
                        "all_sorting_time_seconds": 20 if candidate_id == "first" else 30,
                    }
                ],
            },
            "host_runner": {},
            "errors": [],
        }

    monkeypatch.setattr(api, "post_scenario_generate", fake_scenario)
    monkeypatch.setattr(api, "post_simulation_run", fake_simulation)
    monkeypatch.setattr(
        api,
        "build_evidence_summary",
        lambda **kwargs: {"evidence_summary": {"deployment_allowed": True}},
    )

    api._run_strategy_batch_worker("strategy_batch_test")

    result = repository.load_strategy_batch("strategy_batch_test")
    assert call_order == [
        ("scenario", "candidate_first"),
        ("simulation", "candidate_first"),
        ("scenario", "candidate_second"),
        ("simulation", "candidate_second"),
    ]
    assert result["status"] == "SELECTED"
    assert result["selection"]["selected_candidate_strategy_id"] == "candidate_first"
    assert all(row["timing"]["simulation_startup_seconds"] is None for row in result["candidate_runs"])


def test_orphaned_strategy_batch_becomes_explicit_system_error(monkeypatch, tmp_path):
    repository = TRTRepository(tmp_path)
    monkeypatch.setattr(api, "repository", repository)
    monkeypatch.setattr(api, "STRATEGY_BATCH_WORKERS", {})
    repository.save_strategy_batch(
        {
            "strategy_batch_id": "strategy_batch_orphan",
            "status": "RUNNING",
            "candidate_count": 3,
            "candidate_runs": [],
        }
    )

    result = api.get_strategy_batch("strategy_batch_orphan")

    assert result["status"] == "SYSTEM_ERROR"
    assert "worker is no longer active" in result["error"]
