from tools.m12_ingest_n8n_execution import infer_lifecycle_timestamps, n8n_node_runs


def _entry(start: int, duration: int = 0) -> dict:
    return {"startTime": start, "executionTime": duration}


def test_n8n_lifecycle_uses_parent_run_data_node_names_and_selected_candidate() -> None:
    payload = {
        "run_id": "sim_selected",
        "strategy_selection": {
            "selected_run_id": "sim_selected",
            "ranked_candidates": [
                {
                    "run_id": "sim_other",
                    "scenario_created_at_utc": "2026-08-08T00:00:01Z",
                    "artifact_created_at_utc": "2026-08-08T00:00:02Z",
                },
                {
                    "run_id": "sim_selected",
                    "scenario_created_at_utc": "2026-08-08T00:01:00Z",
                    "artifact_created_at_utc": "2026-08-08T00:03:00Z",
                    "timing": {"isaac_command_started_at_utc": "2026-08-08T00:01:01Z"},
                },
            ],
        },
        "n8n_execution_snapshots": [
            {
                "body": {
                    "createdAt": "2026-06-01T08:01:23.862Z",
                    "data": {
                        "resultData": {
                            "runData": {
                                "Receive Operator Intent": [_entry(1_786_200_000_000)],
                                "Chat Candidate Patch Summary": [_entry(1_786_200_010_000, 25)],
                                "Build Direct Approval Decision Turn": [_entry(1_786_200_020_000, 10)],
                                "Restore Non-Deploy Message After Clear": [_entry(1_786_200_100_000, 15)],
                            }
                        }
                    },
                }
            }
        ],
    }

    runs = n8n_node_runs(payload)
    assert {row["node_name"] for row in runs} == {
        "Receive Operator Intent",
        "Chat Candidate Patch Summary",
        "Build Direct Approval Decision Turn",
        "Restore Non-Deploy Message After Clear",
    }

    lifecycle = infer_lifecycle_timestamps(payload)
    assert lifecycle["INTENT_CREATED"] == "2026-08-08T14:40:00Z"
    assert lifecycle["CANDIDATE_SUMMARY_CREATED"] == "2026-08-08T14:40:10.025000Z"
    assert lifecycle["CANDIDATE_REVIEW_ENDED"] == "2026-08-08T14:40:20.010000Z"
    assert lifecycle["SCENARIO_CREATED"] == "2026-08-08T00:01:00Z"
    assert lifecycle["SIMULATION_STARTED"] == "2026-08-08T00:01:01Z"
    assert lifecycle["RUN_ARTIFACT_CREATED"] == "2026-08-08T00:03:00Z"
    assert lifecycle["DEPLOYMENT_REVIEW_ENDED"] == "2026-08-08T14:41:40.015000Z"
