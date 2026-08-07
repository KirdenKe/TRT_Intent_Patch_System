from tools.m12_generate_reviewed_trial_report import finalized_outcome


def test_successful_negative_case_is_autonomous_test_success():
    assert (
        finalized_outcome(
            recorded="INPUT_FAILURE",
            manual_result="PASS",
            manual_correction_used=False,
            failure_stage="intent_validation",
            failure_cause_code="UNCLEAR_OPERATOR_INPUT",
        )
        == "AUTONOMOUS_SUCCESS"
    )


def test_manual_correction_is_not_counted_as_autonomous_success():
    assert (
        finalized_outcome(
            recorded="EVALUATION_INCOMPLETE",
            manual_result="PASS",
            manual_correction_used=True,
            failure_stage="",
            failure_cause_code="",
        )
        == "MANUALLY_ASSISTED_SUCCESS"
    )


def test_failed_simulation_keeps_simulation_failure_source():
    assert (
        finalized_outcome(
            recorded="EVALUATION_INCOMPLETE",
            manual_result="FAIL",
            manual_correction_used=False,
            failure_stage="isaac_runtime",
            failure_cause_code="SIMULATOR_OR_API_ERROR",
        )
        == "SIMULATION_FAILURE"
    )
