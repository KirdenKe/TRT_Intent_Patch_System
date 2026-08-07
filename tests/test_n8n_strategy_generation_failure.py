import json
from pathlib import Path


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "n8n_workflows"
    / "generate_scenario_spec.workflow.json"
)


def test_candidate_generation_failure_is_terminal_and_does_not_blame_operator():
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in workflow["nodes"]}

    terminal_expression = nodes["Strategy Evaluation Terminal?"]["parameters"][
        "conditions"
    ]["conditions"][0]["leftValue"]
    assert "GENERATION_FAILED" in terminal_expression
    terminal_target = workflow["connections"]["Strategy Evaluation Terminal?"]["main"][0][0][
        "node"
    ]
    assert terminal_target == "Candidate Generation Failed?"

    response_code = nodes["Build Candidate Generation Failure Response"]["parameters"][
        "jsCode"
    ]
    assert "operator intent has not been rejected" in response_code
    assert "operator revision is not required" in response_code
    assert "Isaac Sim was not launched" in response_code

    result_message_code = nodes["Normalize Strategy Result Messaging"]["parameters"]["jsCode"]
    assert "diagnostic, non-blocking" in result_message_code
    assert "SYSTEM_EVALUATION_INCOMPLETE" in result_message_code
    assert "operator revision is not required" in result_message_code
