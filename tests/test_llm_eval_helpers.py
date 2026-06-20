from __future__ import annotations

from pathlib import Path

from trt_core.intent_precheck import deterministic_intent_precheck
from scripts.evaluate_vllm_intent_generator import DATASET_PATH, read_jsonl
from scripts.evaluate_vllm_intent_generator import (
    action_rejects_candidate,
    build_vllm_structured_output_request,
    complete_domain_candidate,
    exact_list_match,
    parse_vllm_content,
    percentile,
    summarize_results,
)


def context() -> dict:
    return {
        "current_trt": {"trt_id": "trt-demo", "version": "v1"},
        "llm_candidate_generation_schema": {
            "type": "object",
            "properties": {
                "line_id": {"type": "string"},
                "goal": {"type": "string"},
                "excluded_instruments": {"type": "array"},
                "action": {"type": "string"},
                "clarification_questions": {"type": "array"},
                "unsupported_terms": {"type": "array"},
                "detected_request_types": {"type": "array"},
            },
            "required": [
                "action",
                "line_id",
                "goal",
                "excluded_instruments",
                "clarification_questions",
                "unsupported_terms",
                "detected_request_types",
            ],
            "additionalProperties": False,
        },
    }


def case() -> dict:
    return {
        "case_id": "eval_test",
        "operator_id": "op_eval",
        "intent_text": "Line 1 trauma priority without forceps.",
        "reason": "urgent",
        "expected_valid": True,
    }


def test_build_vllm_request_uses_simplified_schema_only():
    payload = build_vllm_structured_output_request(case(), context(), "model-a")

    assert payload["model"] == "model-a"
    assert payload["max_tokens"] == 160
    assert payload["structured_outputs"]["json"]["required"] == [
        "action",
        "line_id",
        "goal",
        "excluded_instruments",
        "clarification_questions",
        "unsupported_terms",
        "detected_request_types",
    ]
    assert "operations" not in payload["structured_outputs"]["json"]["properties"]
    assert "Do not generate patch_id" in payload["messages"][0]["content"]
    assert action_rejects_candidate("NEEDS_CLARIFICATION") is True
    assert action_rejects_candidate("UNSUPPORTED_REQUEST") is True
    assert action_rejects_candidate("PROPOSE_PATCH") is False


def test_parse_vllm_content_rejects_length_finish_reason():
    parsed, finish_reason, error = parse_vllm_content(
        {"choices": [{"finish_reason": "length", "message": {"content": "{\"line_id\":\"line_1\""}}]}
    )

    assert parsed is None
    assert finish_reason == "length"
    assert error == "finish_reason_length"


def test_complete_domain_candidate_attaches_metadata():
    candidate = complete_domain_candidate(
        case(),
        context(),
        {"line_id": "line_1", "goal": "TRAUMA_SET_PRIORITY", "excluded_instruments": ["FORCEPS"]},
    )

    assert candidate["patch_id"] == "eval-eval_test"
    assert candidate["trt_id"] == "trt-demo"
    assert candidate["base_version"] == "v1"
    assert candidate["status"] == "REVIEWED"
    assert candidate["line_id"] == "line_1"


def test_summary_rates_and_percentile():
    results = [
        {
            "json_parse_success": True,
            "llm_finish_reason": "stop",
            "schema_valid": True,
            "normalize_success": True,
            "patch_validate_success": True,
            "expected_valid_agrees": True,
            "line_match": True,
            "goal_match": True,
            "excluded_instrument_match": True,
            "latency_seconds": 1.0,
            "completion_tokens": 10,
            "errors": [],
        },
        {
            "json_parse_success": False,
            "llm_finish_reason": "length",
            "latency_seconds": 3.0,
            "completion_tokens": 20,
            "expected_error_type": "ambiguous_request",
            "errors": ["finish_reason_length"],
            "rejection_source": "deterministic_precheck",
        },
    ]

    summary = summarize_results(results)

    assert summary["total_cases"] == 2
    assert summary["json_parse_rate"] == 0.5
    assert summary["finish_reason_length_rate"] == 0.5
    assert summary["clarification_detection_rate"] == 1.0
    assert summary["average_completion_tokens"] == 15
    assert percentile([1.0, 3.0], 0.95) == 2.9
    assert exact_list_match(["FORCEPS", "CLAMPS"], ["CLAMPS", "FORCEPS"]) is True


def test_operator_intent_dataset_has_required_coverage():
    cases = read_jsonl(Path(DATASET_PATH))
    error_types = {case.get("expected_error_type") for case in cases if case.get("expected_error_type")}
    notes = " ".join(case["notes"] for case in cases).lower()

    assert len(cases) >= 30
    assert any(case["expected_valid"] for case in cases)
    assert any(not case["expected_valid"] for case in cases)
    assert "ambiguous_request" in error_types
    assert "invalid_line_reference" in error_types
    assert "invalid_instrument_type" in error_types
    assert "read_only_state_modification" in error_types
    assert "multi_line_request" in error_types
    assert "missing_line_number" in error_types
    assert "missing_goal" in error_types
    assert "synonym" in notes
    assert "case" in notes


def test_deterministic_precheck_detects_invalid_intents():
    current_trt = {"lines": {"line_1": {}, "line_2": {}}}

    invalid_line = deterministic_intent_precheck("Line 9 should prioritize trauma set.", current_trt)
    multi_line = deterministic_intent_precheck("Line 1 and line 2 should clear backlog.", current_trt)
    unsupported = deterministic_intent_precheck("Line 1 should exclude laser scalpel.", current_trt)
    readonly = deterministic_intent_precheck("Pause line 1 and set error mode.", current_trt)
    conflict = deterministic_intent_precheck("Line 1 should be routine and trauma priority.", current_trt)

    assert invalid_line["action"] == "UNSUPPORTED_REQUEST"
    assert "invalid_line" in invalid_line["detected_request_types"]
    assert multi_line["action"] == "NEEDS_CLARIFICATION"
    assert "multi_line_request" in multi_line["detected_request_types"]
    assert unsupported["action"] == "UNSUPPORTED_REQUEST"
    assert "unsupported_instrument" in unsupported["detected_request_types"]
    assert readonly["action"] == "UNSUPPORTED_REQUEST"
    assert "read_only_state_request" in readonly["detected_request_types"]
    assert conflict["action"] == "NEEDS_CLARIFICATION"
    assert "conflicting_goal" in conflict["detected_request_types"]


def test_deterministic_precheck_allows_kpi_throughput_without_goal():
    current_trt = {"lines": {"line_1": {}, "line_2": {}}}

    precheck = deterministic_intent_precheck("set line 1 throughput/hr to 150", current_trt)

    assert precheck["action"] == "PROPOSE_PATCH"
    assert "KPI_LIMIT_UPDATE" in precheck["detected_request_types"]
    assert "missing_goal" not in precheck["detected_request_types"]
    assert precheck["clarification_questions"] == []


def test_deterministic_precheck_rejects_restricted_simulation_settings():
    current_trt = {"lines": {"line_1": {}, "line_2": {}}}

    cases = [
        ("set line 1 layout_source to database", "layout_source"),
        ("set line 1 max_seed_trials to 5", "max_seed_trials"),
        ("set line 1 seed_db_path to c:/tmp/seed.sqlite", "seed_db_path"),
        ("set line 1 reuse_precomputed_layouts true", "reuse_precomputed_layouts"),
    ]

    for intent, term in cases:
        precheck = deterministic_intent_precheck(intent, current_trt)
        assert precheck["action"] == "UNSUPPORTED_REQUEST"
        assert "restricted_simulation_setting" in precheck["detected_request_types"]
        assert term in precheck["unsupported_terms"]
