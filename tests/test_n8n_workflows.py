from __future__ import annotations

import json
from pathlib import Path


WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "n8n_workflows"
CHILD_WORKFLOWS = [
    "intent_to_patch_review.workflow.json",
    "patch_release_approval.workflow.json",
    "released_trt_to_reconciliation.workflow.json",
    "generate_scenario_spec.workflow.json",
]


def load_workflow(name: str) -> dict:
    return json.loads((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def test_child_workflows_start_with_execute_workflow_trigger():
    for workflow_name in CHILD_WORKFLOWS:
        workflow = load_workflow(workflow_name)
        first_node = workflow["nodes"][0]

        assert first_node["type"] == "n8n-nodes-base.executeWorkflowTrigger", workflow_name
        assert "webhookId" not in first_node


def test_child_workflows_do_not_use_respond_to_webhook():
    for workflow_name in CHILD_WORKFLOWS:
        workflow = load_workflow(workflow_name)
        node_types = {node["type"] for node in workflow["nodes"]}

        assert "n8n-nodes-base.respondToWebhook" not in node_types, workflow_name


def test_no_n8n_wait_nodes_for_chat_or_release_human_input():
    for workflow_name in ["chat_operator_task_allocation.workflow.json", "patch_release_approval.workflow.json"]:
        workflow = load_workflow(workflow_name)
        node_types = {node["type"] for node in workflow["nodes"]}
        workflow_text = json.dumps(workflow)

        assert "n8n-nodes-base.wait" not in node_types, workflow_name
        assert "webhook-waiting" not in workflow_text, workflow_name


def test_child_workflow_leaf_outputs_are_normalized_envelopes():
    for workflow_name in CHILD_WORKFLOWS:
        workflow = load_workflow(workflow_name)
        all_node_names = {node["name"] for node in workflow["nodes"]}
        source_nodes = set(workflow["connections"])
        leaf_names = all_node_names - source_nodes
        leaf_nodes = [node for node in workflow["nodes"] if node["name"] in leaf_names]

        assert leaf_nodes, workflow_name
        for leaf in leaf_nodes:
            assert leaf["type"] in {"n8n-nodes-base.set", "n8n-nodes-base.code"}, f"{workflow_name}: {leaf['name']}"
            if leaf["type"] == "n8n-nodes-base.set":
                assignment_names = {
                    assignment["name"]
                    for assignment in leaf["parameters"]["assignments"]["assignments"]
                }
                assert {"status", "context", "payload", "errors"} <= assignment_names, f"{workflow_name}: {leaf['name']}"
            else:
                code = leaf["parameters"]["jsCode"]
                for field in ["status:", "context:", "payload:", "errors:"]:
                    assert field in code, f"{workflow_name}: {leaf['name']}"


def test_integrated_workflow_keeps_webhook_trigger():
    workflow = load_workflow("integrated_operator_task_allocation.workflow.json")
    first_node = workflow["nodes"][0]

    assert first_node["type"] == "n8n-nodes-base.webhook"
    assert first_node["name"] == "Receive Operator Allocation Request"


def test_chat_operator_workflow_starts_with_chat_trigger():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    first_node = workflow["nodes"][0]

    assert first_node["type"] == "@n8n/n8n-nodes-langchain.chatTrigger"
    assert first_node["name"] == "Receive Operator Intent"
    assert first_node["parameters"]["options"]["responseMode"] == "responseNodes"


def test_chat_operator_workflow_classifies_every_chat_turn_with_vllm():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    builder = node_by_name(workflow, "Build vLLM Chat Turn Parse Body")
    llm = node_by_name(workflow, "vLLM Parse Chat Turn")
    normalizer = node_by_name(workflow, "Normalize Parsed Chat Turn")
    route = node_by_name(workflow, "Route Chat Turn")

    assert "Normalize Chat Input" not in {node["name"] for node in workflow["nodes"]}
    assert "Build vLLM Initial Chat Parse Body" not in {node["name"] for node in workflow["nodes"]}
    assert builder["type"] == "n8n-nodes-base.code"
    assert "vllm_body" in builder["parameters"]["jsCode"]
    assert "turn_type" in builder["parameters"]["jsCode"]
    assert "SMALL_TALK" in builder["parameters"]["jsCode"]
    assert "CANCEL" in builder["parameters"]["jsCode"]
    assert "CONFUSED" in builder["parameters"]["jsCode"]
    assert "TASK_REQUEST" in builder["parameters"]["jsCode"]
    assert "CLARIFICATION_VALUES" in builder["parameters"]["jsCode"]
    assert "APPROVAL_DECISION" in builder["parameters"]["jsCode"]
    assert "Do not generate patches" in builder["parameters"]["jsCode"]
    assert "operator_id: op_001 reason: urgent trauma set deadline" in builder["parameters"]["jsCode"]
    assert "Extract reason only from explicit markers" in builder["parameters"]["jsCode"]
    assert "JSON.stringify(examples[0])" in builder["parameters"]["jsCode"]
    assert llm["type"] == "n8n-nodes-base.httpRequest"
    assert llm["parameters"]["jsonBody"] == "={{ $json.vllm_body }}"
    assert normalizer["type"] == "n8n-nodes-base.code"
    assert "JSON.parse(content)" in normalizer["parameters"]["jsCode"]
    assert route["type"] == "n8n-nodes-base.code"


def test_chat_operator_workflow_calls_existing_subworkflows():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    execute_nodes = {
        node["name"]: node["parameters"]["workflowId"]
        for node in workflow["nodes"]
        if node["type"] == "n8n-nodes-base.executeWorkflow"
    }

    assert execute_nodes == {
        "Execute Intent Review Sub-workflow": "IntentToPatchReviewDemo",
        "Execute Release Approval Sub-workflow": "PatchReleaseApprovalDemo",
        "Execute Supervisor Reconciliation Sub-workflow": "ReleasedTRTToReconciliationDemo",
        "Execute ScenarioSpec Generation Sub-workflow": "GenerateScenarioSpecDemo",
    }


def test_chat_operator_workflow_displays_required_summaries():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    summary_nodes = {node["name"] for node in workflow["nodes"] if node["type"] == "n8n-nodes-base.set"}

    assert {
        "Chat Candidate Patch Summary",
        "Chat Release Status Summary",
        "Chat Reconciliation Plan Summary",
        "Chat ScenarioSpec Path Summary",
        "Chat Final Summary",
    } <= summary_nodes


def test_chat_operator_workflow_has_response_formatter_as_final_layer():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    builder = node_by_name(workflow, "Build vLLM User Response Format Body")
    llm = node_by_name(workflow, "vLLM Format User Response")
    normalizer = node_by_name(workflow, "Normalize Formatted User Response")
    code = normalizer["parameters"]["jsCode"]

    assert builder["type"] == "n8n-nodes-base.code"
    assert llm["type"] == "n8n-nodes-base.httpRequest"
    assert llm["parameters"]["jsonBody"] == "={{ $json.vllm_body }}"
    assert normalizer["type"] == "n8n-nodes-base.code"
    assert "user_message" in code
    assert "next_action" in code
    assert "required_fields" in code
    assert "suggested_reply" in code
    assert "debug_json" in code
    assert "raw_json" not in code
    assert "debug" in code
    assert workflow["connections"]["Build Canonical Clarification Payload"]["main"][0][0]["node"] == "Build vLLM User Response Format Body"
    for source in ["Chat Stop Summary", "Chat Final Summary"]:
        assert workflow["connections"][source]["main"][0][0]["node"] == "Build vLLM User Response Format Body"
    assert workflow["connections"]["Build vLLM User Response Format Body"]["main"][0][0]["node"] == "vLLM Format User Response"
    assert workflow["connections"]["vLLM Format User Response"]["main"][0][0]["node"] == "Normalize Formatted User Response"


def test_chat_response_formatter_handles_required_statuses():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    code = node_by_name(workflow, "Build vLLM User Response Format Body")["parameters"]["jsCode"]
    normalizer_code = node_by_name(workflow, "Normalize Formatted User Response")["parameters"]["jsCode"]

    for action in ["PROVIDE_MISSING_FIELDS", "CONFIRM_PATCH", "REVISE_REQUEST", "WAIT", "DONE", "ERROR"]:
        assert action in code


def test_chat_response_formatter_uses_canonical_missing_fields():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    check_code = node_by_name(workflow, "Check Required Chat Fields")["parameters"]["jsCode"]
    formatter_code = node_by_name(workflow, "Normalize Formatted User Response")["parameters"]["jsCode"]

    assert "missing.push('operator_id')" in check_code
    assert "missing.push('intent_text')" in check_code
    assert "missing.push('reason')" in check_code
    assert "Missing required chat field" not in check_code
    assert "required_fields: canonical.missing_fields || []" in formatter_code
    assert "debug_json" in formatter_code
    assert "Missing required chat field" not in formatter_code


def test_chat_workflow_enforces_required_field_gate_before_intent_review():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")

    assert workflow["connections"]["Receive Operator Intent"]["main"][0][0]["node"] == "Build vLLM Chat Turn Parse Body"
    assert workflow["connections"]["Build vLLM Chat Turn Parse Body"]["main"][0][0]["node"] == "vLLM Parse Chat Turn"
    assert workflow["connections"]["vLLM Parse Chat Turn"]["main"][0][0]["node"] == "Normalize Parsed Chat Turn"
    assert workflow["connections"]["Normalize Parsed Chat Turn"]["main"][0][0]["node"] == "Route Chat Turn"
    assert workflow["connections"]["Route Chat Turn"]["main"][0][0]["node"] == "Chat Turn Small Talk?"
    assert workflow["connections"]["Chat Turn Approval Decision?"]["main"][1][0]["node"] == "Check Required Chat Fields"
    assert workflow["connections"]["Check Required Chat Fields"]["main"][0][0]["node"] == "IF Missing Required Fields"
    assert workflow["connections"]["IF Missing Required Fields"]["main"][0][0]["node"] == "Build Canonical Clarification Payload"
    assert workflow["connections"]["IF Missing Required Fields"]["main"][1][0]["node"] == "Execute Intent Review Sub-workflow"
    assert workflow["connections"]["Build Canonical Clarification Payload"]["main"][0][0]["node"] == "Build vLLM User Response Format Body"
    assert workflow["connections"]["Normalize Formatted User Response"]["main"][0][0]["node"] == "Formatted Response Needs Reply?"
    assert workflow["connections"]["Formatted Response Needs Reply?"]["main"][0][0]["node"] == "Ask Clarification Reply"
    assert workflow["connections"]["Ask Clarification Reply"]["main"][0][0]["node"] == "Build vLLM Chat Turn Parse Body"


def test_chat_turn_classifier_routes_non_task_turns_without_required_field_prompt():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")

    assert workflow["connections"]["Chat Turn Small Talk?"]["main"][0][0]["node"] == "Return Small Talk Message"
    assert workflow["connections"]["Chat Turn Cancel?"]["main"][0][0]["node"] == "Return Cancelled Message"
    assert workflow["connections"]["Chat Turn Confused Or Unknown?"]["main"][0][0]["node"] == "Return Confused Chat Turn Message"
    assert workflow["connections"]["Approval Has Reviewed Patch?"]["main"][1][0]["node"] == "Return No Patch Waiting Message"
    for node_name in [
        "Return Small Talk Message",
        "Return Cancelled Message",
        "Return Confused Chat Turn Message",
        "Return No Patch Waiting Message",
    ]:
        assert workflow["connections"][node_name]["main"][0][0]["node"] == "Send Chat Response"


def test_chat_workflow_result_formatter_filters_missing_field_errors():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    code = node_by_name(workflow, "Build vLLM User Response Format Body")["parameters"]["jsCode"]
    normalizer_code = node_by_name(workflow, "Normalize Formatted User Response")["parameters"]["jsCode"]

    assert "cleanErrors" in code
    assert "!value.startsWith('Missing required chat field:')" in code
    assert "missing_fields: missingFields" in code
    assert "debug_json" in normalizer_code


def test_chat_workflow_uses_real_chat_response_node():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    response = node_by_name(workflow, "Send Chat Response")

    assert response["type"] == "@n8n/n8n-nodes-langchain.chat"
    assert response["typeVersion"] == 1
    assert response["parameters"]["message"] == "={{$json.user_message}}"
    assert response["parameters"]["waitUserReply"] is False
    assert "Respond to Chat" not in {node["name"] for node in workflow["nodes"]}
    assert workflow["connections"]["Formatted Response Needs Release Decision?"]["main"][1][0]["node"] == "Send Chat Response"


def test_chat_workflow_waits_inline_for_clarification_and_release_decision():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    clarification = node_by_name(workflow, "Ask Clarification Reply")
    release = node_by_name(workflow, "Ask Release Decision")

    assert clarification["type"] == "@n8n/n8n-nodes-langchain.chat"
    assert clarification["parameters"]["waitUserReply"] is True
    assert clarification["parameters"]["message"] == "={{$json.user_message}}"
    assert release["type"] == "@n8n/n8n-nodes-langchain.chat"
    assert release["parameters"]["waitUserReply"] is True
    assert release["parameters"]["message"].startswith("={{$json.user_message")
    assert workflow["connections"]["Candidate Reviewed?"]["main"][0][0]["node"] == "Build vLLM User Response Format Body"
    assert workflow["connections"]["Formatted Response Needs Release Decision?"]["main"][0][0]["node"] == "Ask Release Decision"
    assert workflow["connections"]["Ask Release Decision"]["main"][0][0]["node"] == "Build vLLM Chat Turn Parse Body"
    assert workflow["connections"]["Approval Has Reviewed Patch?"]["main"][0][0]["node"] == "Normalize Classified Approval Decision"
    assert workflow["connections"]["Normalize Classified Approval Decision"]["main"][0][0]["node"] == "Execute Release Approval Sub-workflow"


def test_chat_approval_decision_uses_classifier_output_not_code_dialogue_parser():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    node_names = {node["name"] for node in workflow["nodes"]}
    builder = node_by_name(workflow, "Build vLLM Chat Turn Parse Body")
    llm = node_by_name(workflow, "vLLM Parse Chat Turn")
    normalizer = node_by_name(workflow, "Normalize Classified Approval Decision")

    assert "Parse Release Decision" not in node_names
    assert "Build vLLM Release Decision Parse Body" not in node_names
    assert builder["type"] == "n8n-nodes-base.code"
    assert "vllm_body" in builder["parameters"]["jsCode"]
    assert "decision" in builder["parameters"]["jsCode"]
    assert "APPROVAL_DECISION" in builder["parameters"]["jsCode"]
    assert "JSON.stringify(examples[0])" in builder["parameters"]["jsCode"]
    assert llm["type"] == "n8n-nodes-base.httpRequest"
    assert llm["parameters"]["jsonBody"] == "={{ $json.vllm_body }}"
    assert normalizer["type"] == "n8n-nodes-base.code"
    code = normalizer["parameters"]["jsCode"]
    assert "operator_decision: operatorDecision" in code
    assert "raw_reply: input.raw_chat_input || null" in code
    assert ".match(" not in code
    assert "indexOf(':')" not in code


def test_chat_clarification_reply_uses_turn_classifier_not_regex_parser():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    llm = node_by_name(workflow, "vLLM Parse Chat Turn")
    normalizer = node_by_name(workflow, "Normalize Parsed Chat Turn")

    assert "Parse Clarification Reply" not in {node["name"] for node in workflow["nodes"]}
    assert "Build vLLM Clarification Parse Body" not in {node["name"] for node in workflow["nodes"]}
    assert llm["type"] == "n8n-nodes-base.httpRequest"
    assert llm["parameters"]["method"] == "POST"
    assert llm["parameters"]["url"] == "http://192.168.50.168:29987/v1/chat/completions"
    body = node_by_name(workflow, "Build vLLM Chat Turn Parse Body")["parameters"]["jsCode"]
    assert "structured_outputs" in body
    assert "operator_id" in body
    assert "CLARIFICATION_VALUES" in body
    code = normalizer["parameters"]["jsCode"]
    assert "JSON.parse(content)" in code
    assert ".match(" not in code


def test_user_response_formatter_prompt_has_status_specific_instructions():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    code = node_by_name(workflow, "Build vLLM User Response Format Body")["parameters"]["jsCode"]

    for text in [
        "NEEDS_CLARIFICATION:",
        "REVIEWED:",
        "RELEASED:",
        "READY / DEGRADED:",
        "GENERATED:",
        "REJECTED / NEEDS_REVISION:",
    ]:
        assert text in code
    assert "Do not decide workflow state." in code
    assert "Examples with valid JSON outputs matching the formatter schema:" in code
    assert "JSON.stringify(formatterExamples[0])" in code


def test_stale_direct_formatter_nodes_are_removed():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    names = {node["name"] for node in workflow["nodes"]}
    combined_targets = {
        target["node"]
        for connection in workflow["connections"].values()
        for output in connection.get("main", [])
        for target in output
    }

    assert "Format Clarification Message" not in names
    assert "Format Workflow Result Message" not in names
    assert "Format Clarification Message" not in combined_targets
    assert "Format Workflow Result Message" not in combined_targets


def test_vllm_chat_turn_http_node_uses_simple_json_body_expression():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    builder = node_by_name(workflow, "Build vLLM Chat Turn Parse Body")
    llm = node_by_name(workflow, "vLLM Parse Chat Turn")
    builder_code = builder["parameters"]["jsCode"]

    assert "vllm_body" in builder_code
    assert "previous_context" in builder_code
    assert "const NL = String.fromCharCode(10);" in builder_code
    assert ".join(NL)" in builder_code
    assert "\\n" not in builder_code
    assert 'content: "User reply:' not in builder_code
    assert llm["parameters"]["jsonBody"] == "={{ $json.vllm_body }}"
    assert "structured_outputs" not in llm["parameters"]["jsonBody"]
    assert "??" not in llm["parameters"]["jsonBody"]
    assert "?." not in llm["parameters"]["jsonBody"]
    assert "{ model:" not in llm["parameters"]["jsonBody"]


def test_release_approval_child_consumes_normalized_candidate_patch_and_decision():
    workflow = load_workflow("patch_release_approval.workflow.json")
    prepare = node_by_name(workflow, "Prepare Pending Release")
    record = node_by_name(workflow, "Record Release Decision")
    normalize = node_by_name(workflow, "Normalize Release Approval Input")

    assert workflow["connections"]["Receive Reviewed Candidate Patch"]["main"][0][0]["node"] == "Normalize Release Approval Input"
    assert workflow["connections"]["Normalize Release Approval Input"]["main"][0][0]["node"] == "Candidate Patch Present?"
    assert workflow["connections"]["Candidate Patch Present?"]["main"][0][0]["node"] == "Prepare Pending Release"
    assert workflow["connections"]["Candidate Patch Present?"]["main"][1][0]["node"] == "Missing Candidate Patch Output"
    assert "candidate_patch" in normalize["parameters"]["jsCode"]
    assert prepare["parameters"]["jsonBody"] == "={{ $json.candidate_patch }}"
    assert "$('Prepare Pending Release').first().json.release_id" in record["parameters"]["jsonBody"]
    assert "$('Normalize Release Approval Input').first().json.decision" in record["parameters"]["jsonBody"]
    assert "Receive Reviewed Candidate Patch" not in record["parameters"]["jsonBody"]
    assert "$json.body" not in prepare["parameters"]["jsonBody"]


def test_revision_and_rejected_notification_outputs_are_code_nodes_with_array_errors():
    workflow = load_workflow("patch_release_approval.workflow.json")
    for name, status_text in [
        ("Revision Notification Output", "status: 'NEEDS_REVISION'"),
        ("Rejected Notification Output", "'REJECTED_BY_OPERATOR'"),
    ]:
        output = node_by_name(workflow, name)
        code = output["parameters"]["jsCode"]

        assert output["type"] == "n8n-nodes-base.code"
        assert "const comment =" in code
        assert status_text in code
        assert "errors: [comment]" in code


def node_by_name(workflow: dict, name: str) -> dict:
    return next(node for node in workflow["nodes"] if node["name"] == name)


def assignment_value(node: dict, field_name: str) -> str:
    return next(
        assignment["value"]
        for assignment in node["parameters"]["assignments"]["assignments"]
        if assignment["name"] == field_name
    )


def test_release_approval_preserves_context_and_adds_release_fields():
    workflow = load_workflow("patch_release_approval.workflow.json")
    released = node_by_name(workflow, "Released Notification Output")
    context_expr = assignment_value(released, "context")

    assert "...($('Normalize Release Approval Input').first().json.context || {})" in context_expr
    assert "release_id: $('Prepare Pending Release').item.json.release_id" in context_expr
    assert "audit_id: $json.audit_id" in context_expr
    assert "trt_id:" in context_expr
    assert "trt_version:" in context_expr


def test_reconciliation_preserves_release_id_and_adds_reconciliation_plan_id():
    workflow = load_workflow("released_trt_to_reconciliation.workflow.json")
    ready = node_by_name(workflow, "Return Ready Plan")
    context_expr = assignment_value(ready, "context")

    assert "release_id: $('Receive Released TRT').first().json.context?.release_id || null" in context_expr
    assert "reconciliation_plan_id: $json.plan_id" in context_expr
    assert "trt_id: $('Receive Released TRT').first().json.context?.trt_id || $json.trt_id" in context_expr
    assert "trt_version: $('Receive Released TRT').first().json.context?.trt_version || $json.trt_version" in context_expr


def test_integrated_workflow_has_context_normalizer_after_each_subworkflow():
    workflow = load_workflow("integrated_operator_task_allocation.workflow.json")
    expected_pairs = {
        "Execute Intent Review Sub-workflow": "Normalize Context After Intent Review",
        "Execute Release Approval Sub-workflow": "Normalize Context After Release Approval",
        "Execute Supervisor Reconciliation Sub-workflow": "Normalize Context After Reconciliation",
        "Execute ScenarioSpec Generation Sub-workflow": "Normalize Context After ScenarioSpec Generation",
    }

    for execute_node, normalize_node in expected_pairs.items():
        connections = workflow["connections"][execute_node]["main"][0]
        assert connections[0]["node"] == normalize_node
        normalizer = node_by_name(workflow, normalize_node)
        assert normalizer["type"] == "n8n-nodes-base.code"
        code = normalizer["parameters"]["jsCode"]
        assert "function firstNonNull" in code
        assert "ids.release_id" in code
        assert "previous.release_id" in code


def test_context_normalizer_does_not_overwrite_existing_release_id_with_null_ids():
    workflow = load_workflow("integrated_operator_task_allocation.workflow.json")
    normalizer = node_by_name(workflow, "Normalize Context After Reconciliation")
    code = normalizer["parameters"]["jsCode"]

    assert "Object.entries(ids).filter(([_, v]) => v !== null && v !== undefined && v !== \"\")" in code
    assert "release_id: firstNonNull(\n          current.context?.release_id,\n          ids.release_id,\n          previous.release_id\n        )" in code


def test_generate_scenario_spec_request_body_uses_normalized_request():
    workflow = load_workflow("generate_scenario_spec.workflow.json")
    normalizer = node_by_name(workflow, "Normalize ScenarioSpec Generation Input")
    request_node = node_by_name(workflow, "Generate ScenarioSpec")
    body = request_node["parameters"]["jsonBody"]
    code = normalizer["parameters"]["jsCode"]

    assert body == "={{ $json.scenario_request }}"
    assert "scenario_request" in code
    assert "candidate_strategy_id: 'primary'" in code
    assert "scenario_template_id: 'surgical_sorting_v1'" in code
    assert "include_waiting_scenarios: false" in code
    assert "affected_lines: context.affected_lines || []" in code
    assert "const plan = input.payload?.plan || {};" in code
    assert "ids.release_id" not in body


def test_generate_scenario_spec_has_defensive_context_validation():
    workflow = load_workflow("generate_scenario_spec.workflow.json")
    normalizer = node_by_name(workflow, "Normalize ScenarioSpec Generation Input")
    code = normalizer["parameters"]["jsCode"]

    assert "Missing ScenarioSpec generation fields:" in code
    assert "status: 'REJECTED'" in code
    assert "errors: [message]" in code
    assert workflow["connections"]["Generate Baseline Scenario?"]["main"][0][0]["node"] == "Normalize ScenarioSpec Generation Input"
    assert workflow["connections"]["Normalize ScenarioSpec Generation Input"]["main"][0][0]["node"] == "ScenarioSpec Request Complete?"
    assert workflow["connections"]["ScenarioSpec Request Complete?"]["main"][0][0]["node"] == "Generate ScenarioSpec"
    assert workflow["connections"]["ScenarioSpec Request Complete?"]["main"][1][0]["node"] == "Return Missing ScenarioSpec Fields"


def test_generate_scenario_spec_detects_no_change_line_decisions_from_all_paths():
    workflow = load_workflow("generate_scenario_spec.workflow.json")
    validator = node_by_name(workflow, "Validate ScenarioSpec Context")
    code = validator["parameters"]["jsCode"]

    assert "payload.line_decisions" in code
    assert "payload.plan?.line_decisions" in code
    assert "current.line_decisions" in code
    assert "current.plan?.line_decisions" in code
    assert "lineDecisions.every(item => item.decision === 'NO_CHANGE')" in code
    assert "evaluatesReleasedPatchImpact" in code
    assert "status: 'NO_EFFECT'" in code
    assert "payload: { ...payload, plan: normalizedPlan }" in code


def test_generate_scenario_spec_derives_affected_lines_from_candidate_patch_operations():
    workflow = load_workflow("generate_scenario_spec.workflow.json")
    code = node_by_name(workflow, "Validate ScenarioSpec Context")["parameters"]["jsCode"]

    assert "function affectedLinesFromOperations" in code
    assert "const path = operation.path || '';" in code
    assert "const lineIndex = parts.indexOf('lines');" in code
    assert "affected_lines: affectedLines" in code


def test_no_change_only_reconciliation_returns_no_effect_by_default():
    workflow = load_workflow("generate_scenario_spec.workflow.json")
    validator = node_by_name(workflow, "Validate ScenarioSpec Context")
    code = validator["parameters"]["jsCode"]

    assert "onlyNoChange" in code
    assert "allow_baseline_on_no_change" in code
    assert "status: 'NO_EFFECT'" in code
    assert "Reconciliation plan contains no changed lines." in code
    assert node_by_name(workflow, "Return No Effect")


def test_no_workflow_requires_manual_release_or_reconciliation_id_entry():
    combined = "\n".join(
        (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        for name in [
            "integrated_operator_task_allocation.workflow.json",
            "patch_release_approval.workflow.json",
            "released_trt_to_reconciliation.workflow.json",
            "generate_scenario_spec.workflow.json",
        ]
    ).lower()

    assert "manual" not in combined or "manual_clearance_required" in combined
    assert "enter release_id" not in combined
    assert "enter reconciliation_plan_id" not in combined
