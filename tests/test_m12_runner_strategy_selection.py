from tools.m12_run_full_n8n_tests import extract_strategy_selection


def test_extracts_explicit_selected_run_instead_of_first_nested_run():
    payload = {
        "unrelated": {"run_id": "sim_first_but_not_selected"},
        "payload": {
            "strategy_batch_id": "strategy_batch_001",
            "candidate_count": 2,
            "candidate_runs": [
                {"candidate_strategy_id": "candidate_a", "run_id": "sim_candidate_a"},
                {"candidate_strategy_id": "candidate_b", "run_id": "sim_candidate_b"},
            ],
            "selection": {
                "status": "SELECTED",
                "selected_candidate_strategy_id": "candidate_b",
                "selected_scenario_spec_id": "scn_candidate_b",
                "selected_run_id": "sim_candidate_b",
                "objective": {"objective_id": "constraint_gated_throughput_v2"},
                "objective_score": 1.2,
                "ranked_candidates": [],
                "post_simulation_regeneration_performed": False,
            },
        },
    }

    result = extract_strategy_selection(payload)

    assert result["selected_run_id"] == "sim_candidate_b"
    assert result["selected_scenario_spec_id"] == "scn_candidate_b"
    assert result["candidate_run_ids"] == ["sim_candidate_a", "sim_candidate_b"]
    assert result["objective_id"] == "constraint_gated_throughput_v2"


def test_extracts_no_eligible_batch_for_refinement_audit():
    payload = {
        "strategy_batch_id": "strategy_batch_none",
        "selection": {
            "status": "NO_ELIGIBLE_STRATEGY",
            "selected_candidate_strategy_id": None,
            "ranked_candidates": [],
            "operator_refinement_required": True,
            "post_simulation_regeneration_performed": False,
        },
    }

    result = extract_strategy_selection(payload)

    assert result["strategy_batch_id"] == "strategy_batch_none"
    assert result["operator_refinement_required"] is True
    assert result["post_simulation_regeneration_performed"] is False
