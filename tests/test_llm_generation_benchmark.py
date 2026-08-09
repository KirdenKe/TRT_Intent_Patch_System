from __future__ import annotations

import csv
import json

import tools.llm_generation_benchmark as benchmark


def _decision() -> dict:
    return {
        "dialogue_state": "NEEDS_CLARIFICATION",
        "turn_type": "TASK_REQUEST",
        "operator_message": "Operator ID and reason are required.",
        "normalized_request": {
            "operator_id": None,
            "reason": None,
            "intent_text": "set line 1 throughput/hr to at least 90 and simulate four production lines with 2 tooling per line",
            "target_scope": "SINGLE_LINE",
            "target_lines": ["line_1"],
            "target_set_id": None,
            "request_types": ["KPI_UPDATE", "SIMULATION_CONFIG_UPDATE"],
            "kpi_updates": {"min_throughput_per_hour": 90},
            "manipulator_priority": None,
            "simulation_config_updates": {
                "num_envs": 4,
                "add_reference_number": 2,
            },
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


def test_tc7_preserves_direct_cross_model_request_provenance(monkeypatch, tmp_path):
    def fake_post(url, body, timeout_seconds):
        return {
            "choices": [{"message": {"content": json.dumps(_decision())}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

    monkeypatch.setattr(benchmark, "post_json", fake_post)
    manifest = benchmark.run_benchmark(
        rows=[
            {
                "id": "TC7_FIXTURE_001",
                "operator_text": "set line 1 throughput/hr to at least 90 and simulate four production lines with 2 tooling per line",
                "expected_request_types": ["KPI_UPDATE", "SIMULATION_CONFIG_UPDATE"],
                "expected_target_lines": ["line_1"],
                "expected_kpi_updates": {"min_throughput_per_hour": 90},
                "expected_simulation_config_updates": {"num_envs": 4, "add_reference_number": 2},
            }
        ],
        repetitions=2,
        output=tmp_path,
        timeout_seconds=1,
        hardware_description="test hardware",
    )

    request_rows = [
        json.loads(line)
        for line in (tmp_path / "llm_generation_requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(request_rows) == 3
    assert len({row["prompt_sha256"] for row in request_rows}) == 1
    assert len({row["schema_sha256"] for row in request_rows}) == 1
    assert len({row["request_sha256"] for row in request_rows}) == 3
    assert manifest["benchmark_scope"] == "DIRECT_CROSS_MODEL_STRUCTURED_GENERATION"
    assert manifest["n8n_used"] is False
    assert manifest["isaac_sim_used"] is False
    assert manifest["deployment_attempted"] is False

    with (tmp_path / "tc7_model_comparison_results.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert {row["manual_semantic_review_status"] for row in rows} == {
        "PENDING_MANUAL_REVIEW"
    }
    assert {row["repetitions_per_fixture"] for row in rows} == {"2"}
