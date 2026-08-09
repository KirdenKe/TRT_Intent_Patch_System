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


def workflow_connection_target(workflow: dict, source: str) -> str:
    return workflow["connections"][source]["main"][0][0]["node"]


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


def test_workflow_connection_main_entries_are_nested_arrays():
    for workflow_name in [
        *CHILD_WORKFLOWS,
        "chat_operator_task_allocation.workflow.json",
        "integrated_operator_task_allocation.workflow.json",
    ]:
        workflow = load_workflow(workflow_name)
        for source, connection in workflow["connections"].items():
            for output_index, output in enumerate(connection.get("main", [])):
                assert isinstance(output, list), f"{workflow_name}: {source}.main[{output_index}]"


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
    assert "including no, cancel, stop, never mind, or abort" in builder["parameters"]["jsCode"]
    assert "explicitly says APPROVE, REJECT, or REQUEST_REVISION" in builder["parameters"]["jsCode"]
    assert 'input: "no"' in builder["parameters"]["jsCode"]
    assert 'turn_type: "CANCEL"' in builder["parameters"]["jsCode"]
    assert "Do not generate patches" in builder["parameters"]["jsCode"]
    assert "operator_id: op_001 reason: urgent trauma set deadline" in builder["parameters"]["jsCode"]
    assert "Extract reason only from explicit markers" in builder["parameters"]["jsCode"]
    assert "JSON.stringify(examples[0])" in builder["parameters"]["jsCode"]
    assert llm["type"] == "n8n-nodes-base.httpRequest"
    assert llm["parameters"]["jsonBody"] == "={{ $json.vllm_body }}"
    assert normalizer["type"] == "n8n-nodes-base.code"
    assert "JSON.parse(content)" in normalizer["parameters"]["jsCode"]
    assert route["type"] == "n8n-nodes-base.code"


def test_chat_workflow_preserves_pending_intent_for_clarification_turns():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    workflow_text = json.dumps(workflow)
    builder = node_by_name(workflow, "Build vLLM Chat Turn Parse Body")
    normalizer = node_by_name(workflow, "Normalize Parsed Chat Turn")
    load_session = node_by_name(workflow, "Load Chat Session State")
    pending_gate = node_by_name(workflow, "Pending Clarification?")
    required_gate = node_by_name(workflow, "Pending Required Fields?")
    required_merge = node_by_name(workflow, "Merge Pending Required Fields")
    merge_session = node_by_name(workflow, "Merge Pending Clarification")
    merged_normalizer = node_by_name(workflow, "Normalize Merged Clarification")
    save_pending = node_by_name(workflow, "Save Pending Clarification Session State")
    builder_code = builder["parameters"]["jsCode"]
    normalizer_code = normalizer["parameters"]["jsCode"]

    assert "$getWorkflowStaticData" not in workflow_text
    assert "pending_intents" not in workflow_text
    assert load_session["type"] == "n8n-nodes-base.httpRequest"
    assert "/chat/session/{{$json.session_id}}" in load_session["parameters"]["url"]
    assert pending_gate["type"] == "n8n-nodes-base.if"
    assert "WAITING_FOR_CLARIFICATION" in json.dumps(pending_gate["parameters"])
    assert merge_session["type"] == "n8n-nodes-base.httpRequest"
    assert "/chat/session/{{$json.context.session_id}}/merge-clarification" in merge_session["parameters"]["url"]
    assert save_pending["type"] == "n8n-nodes-base.httpRequest"
    assert "WAITING_FOR_CLARIFICATION" in save_pending["parameters"]["jsonBody"]
    assert "pending_intent" in builder_code
    assert "loadedSession.pending_intent" in builder_code
    assert "chat_session_state" in builder_code
    assert "chat_session_state: chatSessionState" in normalizer_code
    assert "pending_intent: pendingIntent" in normalizer_code
    assert required_gate["type"] == "n8n-nodes-base.if"
    assert "WAITING_FOR_REQUIRED_FIELDS" in json.dumps(required_gate["parameters"])
    assert "original_intent_text" in required_merge["parameters"]["jsCode"]
    assert "intent_text: originalIntent" in required_merge["parameters"]["jsCode"]
    assert "operator_id: operatorId" in required_merge["parameters"]["jsCode"]
    assert "resolves an unresolved pending intent" in builder_code
    assert "without replacing the original request" in builder_code
    assert "If pending intent exists" in builder_code
    assert "CLARIFICATION_VALUES" in builder_code
    assert "explicitNewRequest" in normalizer_code
    assert "explicitCancel" in normalizer_code
    assert "pendingIntent && !explicitNewRequest && !explicitCancel && !explicitApproval" in normalizer_code
    assert "originalPendingText" in normalizer_code
    assert "pendingIntent?.operator_id" in normalizer_code
    assert "pendingIntent?.reason" in normalizer_code
    assert "Clarification:" in normalizer_code
    assert "candidate_patch" in merged_normalizer["parameters"]["jsCode"]
    assert "simulation_config_updates" in merged_normalizer["parameters"]["jsCode"]
    assert "$json.chat_session_state === 'WAITING_FOR_CLARIFICATION'" in json.dumps(pending_gate["parameters"])


def test_chat_operator_workflow_saves_required_field_state_and_clears_cancel():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    save_required = node_by_name(workflow, "Save Required Field Session State")
    clear_session = node_by_name(workflow, "Clear Chat Session State")
    clear_completed = node_by_name(workflow, "Clear Completed Chat Session State")
    formatted_reply_gate = node_by_name(workflow, "Formatted Response Needs Reply?")
    cancel_code = node_by_name(workflow, "Return Cancelled Message")["parameters"]["jsCode"]

    assert save_required["type"] == "n8n-nodes-base.httpRequest"
    assert save_required["parameters"]["method"] == "PUT"
    assert "/chat/session/" in save_required["parameters"]["url"]
    assert "WAITING_FOR_REQUIRED_FIELDS" in save_required["parameters"]["jsonBody"]
    assert "original_intent_text" in save_required["parameters"]["jsonBody"]
    assert "raw_backend_response?.context" in save_required["parameters"]["jsonBody"]
    assert "simulation_config_updates" in save_required["parameters"]["jsonBody"]
    assert "kpi_updates" in save_required["parameters"]["jsonBody"]
    assert clear_session["type"] == "n8n-nodes-base.httpRequest"
    assert clear_session["parameters"]["method"] == "DELETE"
    assert "/chat/session/" in clear_session["parameters"]["url"]
    assert clear_completed["type"] == "n8n-nodes-base.httpRequest"
    assert clear_completed["parameters"]["method"] == "DELETE"
    assert "/chat/session/" in clear_completed["parameters"]["url"]
    assert "PROVIDE_CLARIFICATION" in json.dumps(formatted_reply_gate["parameters"])
    clarification_edges = workflow["connections"]["Ask Clarification Reply"]["main"][0]
    approval_edges = workflow["connections"]["Ask Release Decision"]["main"][0]
    assert any(edge["node"] == "Extract Session ID" for edge in clarification_edges)
    assert any(edge["node"] == "Extract Session ID" for edge in approval_edges)
    assert "$getWorkflowStaticData" not in cancel_code


def test_intent_workflow_supports_simulation_config_update_candidate():
    workflow = load_workflow("intent_to_patch_review.workflow.json")
    body = node_by_name(workflow, "LLM Generate Intent Patch")["parameters"]["jsonBody"]
    retry_body = node_by_name(workflow, "Retry LLM Generate Intent Patch")["parameters"]["jsonBody"]
    normalizer_code = node_by_name(workflow, "Normalize Candidate Patch")["parameters"]["jsCode"]
    retry_normalizer_code = node_by_name(workflow, "Normalize Retried Candidate Patch")["parameters"]["jsCode"]
    reviewed_code = node_by_name(workflow, "Return Reviewed Candidate")["parameters"]["jsCode"]

    for prompt in [body, retry_body]:
        assert "SIMULATION_CONFIG_UPDATE" in prompt
        assert "MANIPULATOR_PRIORITY_UPDATE" in prompt
        assert "manipulator_priority" in prompt
        assert "add_reference_number" in prompt
        assert "simulation_config_updates" in prompt
        assert "Do not ask which five tools unless the user names specific tools to keep." in prompt
    for code in [normalizer_code, retry_normalizer_code]:
        assert "sourceInput.pending_intent?.simulation_config_updates" in code
        assert "sourceInput.loaded_session?.normalized_request?.simulation_config_updates" in code
        assert "...preservedSimulationConfig" in code
        assert "...(extracted.simulation_config_updates || {})" in code
    assert "manipulator_priority: extracted.manipulator_priority ?? null" in normalizer_code
    assert "manipulator_priority: extracted.manipulator_priority ?? null" in retry_normalizer_code
    assert "const simulationConfigUpdates = candidatePatch.simulation_config_updates || null;" in reviewed_code
    assert "simulation_config_updates: simulationConfigUpdates" in reviewed_code
    assert "The candidate simulation configuration update is valid" in reviewed_code


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


def test_intent_candidate_vllm_budget_and_length_guard():
    workflow = load_workflow("intent_to_patch_review.workflow.json")
    generator = node_by_name(workflow, "LLM Generate Intent Patch")
    retry_generator = node_by_name(workflow, "Retry LLM Generate Intent Patch")
    normalizer = node_by_name(workflow, "Normalize Candidate Patch")
    retry_normalizer = node_by_name(workflow, "Normalize Retried Candidate Patch")
    body = generator["parameters"]["jsonBody"]
    retry_body = retry_generator["parameters"]["jsonBody"]
    code = normalizer["parameters"]["jsCode"]
    retry_code = retry_normalizer["parameters"]["jsCode"]

    assert "max_tokens: 20000" in body
    assert "max_tokens: 200000" in retry_body
    assert "temperature: 0.0" in body
    assert "structured_outputs" in body
    assert "Extract compact domain intent JSON only." in body
    assert "Time-Arrival Model language is supported" in body
    assert "Use top-level unsupported_terms only" in body
    assert "Do not duplicate unsupported_terms inside sub_requests" in body
    assert "Allowed lines come from Get Intent Context valid_line_ids." in body
    assert "Allowed goals: ROUTINE_CLASSIFICATION,TRAUMA_SET_PRIORITY,BACKLOG_CLEARING." in body
    assert "Allowed normalized tooling types are:" in body
    assert "Known tooling aliases are:" in body
    assert "Knife handle/knife handles maps to KNIFE_HANDLE." in body
    assert "Allowed abnormal strategies: STOP_LINE,CONTINUE_FEASIBLE_TASKS,ASK_OPERATOR." in body
    assert "target_scope: single line=SINGLE_LINE" in body
    assert "no deadline" in body
    assert "no maximum downtime limit" in body
    assert "all tooling required" in body
    assert "Action rules:" in body
    assert "clarification_questions must be short" not in body
    assert "Do not write long explanatory sentences." not in body
    assert "finishReason === 'length'" in code
    assert "RETRY_INTENT_GENERATION" in code
    assert "retry_needed: true" in code
    assert "Please retry with a shorter request" not in code
    assert "JSON.parse(content)" in code
    assert "INTENT_LLM_TRUNCATED_AFTER_RETRIES" in retry_code
    assert "llm_action: 'ERROR'" in retry_code
    assert "This is a system issue, not an operator request issue" in retry_code
    assert "endpoint_url: 'http://192.168.50.168:29987/v1/chat/completions'" in retry_code
    assert "requested_max_tokens: 200000" in retry_code
    assert "tooling_policy_updates: extracted.tooling_policy_updates || null" in code
    assert "manipulator_priority_updates: extracted.manipulator_priority_updates || null" in code
    assert "tooling_policy_updates: extracted.tooling_policy_updates || null" in retry_code
    assert "manipulator_priority_updates: extracted.manipulator_priority_updates || null" in retry_code


def test_intent_candidate_retry_branch_reaches_python_normalization_on_success():
    workflow = load_workflow("intent_to_patch_review.workflow.json")

    assert workflow["connections"]["Normalize Candidate Patch"]["main"][0][0]["node"] == "Intent Candidate Needs Retry?"
    assert workflow["connections"]["Intent Candidate Needs Retry?"]["main"][0][0]["node"] == "Retry LLM Generate Intent Patch"
    assert workflow["connections"]["Intent Candidate Needs Retry?"]["main"][1][0]["node"] == "LLM Proposes Patch?"
    assert workflow["connections"]["Retry LLM Generate Intent Patch"]["main"][0][0]["node"] == "Normalize Retried Candidate Patch"
    assert workflow["connections"]["Normalize Retried Candidate Patch"]["main"][0][0]["node"] == "LLM Proposes Patch?"
    assert workflow["connections"]["LLM Proposes Patch?"]["main"][0][0]["node"] == "Normalize Domain Candidate with Python"


def test_intent_prompts_cover_explicit_time_values_and_known_composite_semantics():
    workflow = load_workflow("intent_to_patch_review.workflow.json")
    for node_name in ("LLM Generate Intent Patch", "Retry LLM Generate Intent Patch"):
        body = node_by_name(workflow, node_name)["parameters"]["jsonBody"]
        assert "Explicit absolute values" in body
        assert "continue until I arrive maps to chosen_intervention_mode" in body
        assert 'selected_normalized_types=["KNIFE_HANDLE"]' in body
        assert 'policy:"UNWANTED_FIRST"' in body


def test_intent_python_business_rule_rejections_are_normalized_not_failed():
    workflow = load_workflow("intent_to_patch_review.workflow.json")
    http_node = node_by_name(workflow, "Normalize Domain Candidate with Python")
    normalizer = node_by_name(workflow, "Normalize Python Candidate Result")
    router = node_by_name(workflow, "Python Candidate Accepted?")
    normalizer_code = normalizer["parameters"]["jsCode"]

    assert http_node["continueOnFail"] is True
    assert normalizer["type"] == "n8n-nodes-base.code"
    assert router["type"] == "n8n-nodes-base.if"
    assert "const status = systemFailure ? 'ERROR' : 'REJECTED'" in normalizer_code
    assert "Retry the same request unchanged" in normalizer_code
    assert "status: 'NORMALIZED'" in normalizer_code
    condition = router["parameters"]["conditions"]["conditions"][0]
    assert condition["rightValue"] == "NORMALIZED"
    assert condition["operator"]["operation"] == "equals"
    assert "Normalize Domain Candidate with Python" in normalizer_code
    assert "line_2 is currently in ERROR mode" in normalizer_code
    assert workflow["connections"]["Normalize Domain Candidate with Python"]["main"][0][0]["node"] == "Normalize Python Candidate Result"
    assert workflow["connections"]["Normalize Python Candidate Result"]["main"][0][0]["node"] == "Python Candidate Accepted?"
    assert workflow["connections"]["Python Candidate Accepted?"]["main"][0][0]["node"] == "Validate Candidate Patch"
    assert workflow["connections"]["Python Candidate Accepted?"]["main"][1][0]["node"] == "Return Python Candidate Rejection"


def test_intent_candidate_retry_supports_old_and_all_lines_requests_without_user_retry_message():
    workflow = load_workflow("intent_to_patch_review.workflow.json")
    body = node_by_name(workflow, "LLM Generate Intent Patch")["parameters"]["jsonBody"]
    retry_body = node_by_name(workflow, "Retry LLM Generate Intent Patch")["parameters"]["jsonBody"]
    normalizer_code = node_by_name(workflow, "Normalize Candidate Patch")["parameters"]["jsCode"]

    assert "Line 1" not in body or "line_1" in body
    assert "all/every/each production line=ALL_LINES" in body
    assert "TRAUMA_SET_PRIORITY" in body
    assert "FORCEPS" in body
    assert "kpi_updates.deadline_minutes=null" in body
    assert "kpi_updates.max_downtime_seconds=null" in body
    assert "PRIORITY_UPDATE" in body
    assert "priority to the highest level => priority=5" in body
    assert "lowest priority => priority=1" in body
    assert "Do not map priority language to goal." in body
    assert "Do not set goal=TRAUMA_SET_PRIORITY unless the user explicitly asks for Trauma Set priority." in body
    assert "Explicit absolute values" in body
    assert "continue until I arrive maps to chosen_intervention_mode" in body
    assert 'selected_normalized_types=["KNIFE_HANDLE"]' in body
    assert 'policy:"UNWANTED_FIRST"' in body
    assert "allowed_instruments is selected tooling for the strategy, not robot capability." in body
    assert "select no tooling or do not want all tooling selected => tooling_policy.required_scope=NONE" in body
    assert "select all tooling => tooling_policy.required_scope=ALL_SUPPORTED_TOOLING" in body
    assert "all tooling required by each production line" in body
    assert "tooling_policy.required_scope=ALL_SUPPORTED_TOOLING" in body
    assert "Entanglement is not an instrument exclusion" in body
    assert "Return action,line_id,target_scope,target_lines,goal,priority,allowed_instruments,excluded_instruments,selected_normalized_types,excluded_normalized_types" in body
    assert "priority: extracted.priority ?? null" in normalizer_code
    assert "excluded_normalized_types: extracted.excluded_normalized_types ?? null" in normalizer_code
    assert "tooling_policy.all_required" not in body
    assert "tooling_policy.all_required" not in retry_body
    assert retry_body.count("max_tokens: 200000") == 1
    assert "Please retry with a shorter request" not in normalizer_code


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
    assert workflow["connections"]["Ask Release Decision"]["main"][0][0]["node"] == "Extract Session ID"
    assert workflow["connections"]["Approval Has Reviewed Patch?"]["main"][0][0]["node"] == "Normalize Classified Approval Decision"
    assert workflow["connections"]["Normalize Classified Approval Decision"]["main"][0][0]["node"] == "Canonicalize Candidate Patch For Approval"
    assert workflow["connections"]["Canonicalize Candidate Patch For Approval"]["main"][0][0]["node"] == "Execute Release Approval Sub-workflow"


def test_chat_approval_decision_uses_classifier_output_not_code_dialogue_parser():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    node_names = {node["name"] for node in workflow["nodes"]}
    builder = node_by_name(workflow, "Build vLLM Dialogue Decision Body")
    llm = node_by_name(workflow, "Call vLLM Dialogue Decision")
    normalizer = node_by_name(workflow, "Normalize Classified Approval Decision")

    assert "Parse Release Decision" not in node_names
    assert "Build vLLM Release Decision Parse Body" not in node_names
    assert builder["type"] == "n8n-nodes-base.code"
    assert "latest_user_message" in builder["parameters"]["jsCode"]
    assert "active_request" in builder["parameters"]["jsCode"]
    assert llm["type"] == "n8n-nodes-base.httpRequest"
    assert llm["parameters"]["url"] == "http://trt-api:8000/chat/dialogue-decision"
    assert llm["parameters"]["jsonBody"] == "={{ $json }}"
    assert normalizer["type"] == "n8n-nodes-base.code"
    code = normalizer["parameters"]["jsCode"]
    assert "operator_decision: operatorDecision" in code
    assert "raw_reply: input.raw_chat_input || null" in code
    assert ".match(" not in code
    assert "indexOf(':')" not in code


def test_chat_approval_canonicalizes_candidate_patch_before_release():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    canonical = node_by_name(workflow, "Canonicalize Candidate Patch For Approval")

    assert canonical["type"] == "n8n-nodes-base.code"
    code = canonical["parameters"]["jsCode"]
    assert "affectedLinesFromOperations" in code
    assert "input.payload?.candidate_patch" in code
    assert "input.body?.candidate_patch" in code
    assert "input.candidate_patch" in code
    assert "input.patch_id && Array.isArray(input.operations)" in code
    assert "candidate_patch: reconstructedCandidate" in code
    assert "simulation_config_updates: simulationConfigUpdates" in code
    assert "Approval path invariant failed: candidate_patch is missing after canonicalization" in code
    assert workflow["connections"]["Normalize Classified Approval Decision"]["main"][0][0]["node"] == (
        "Canonicalize Candidate Patch For Approval"
    )
    assert workflow["connections"]["Canonicalize Candidate Patch For Approval"]["main"][0][0]["node"] == (
        "Execute Release Approval Sub-workflow"
    )


def test_chat_clarification_reply_uses_turn_classifier_not_regex_parser():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    llm = node_by_name(workflow, "Call vLLM Dialogue Decision")
    normalizer = node_by_name(workflow, "Normalize Dialogue Decision")

    assert "Parse Clarification Reply" not in {node["name"] for node in workflow["nodes"]}
    assert "Build vLLM Clarification Parse Body" not in {node["name"] for node in workflow["nodes"]}
    assert llm["type"] == "n8n-nodes-base.httpRequest"
    assert llm["parameters"]["method"] == "POST"
    assert llm["parameters"]["url"] == "http://trt-api:8000/chat/dialogue-decision"
    body = node_by_name(workflow, "Build vLLM Dialogue Decision Body")["parameters"]["jsCode"]
    assert "loaded_session" in body
    assert "prior_clarification_questions" in body
    code = normalizer["parameters"]["jsCode"]
    assert "dialogue_state" in code
    assert ".match(" not in code
    assert '"DEPLOYMENT_DECISION"' in code
    assert 'turnType = "CLARIFICATION_VALUES"' in code


def test_chat_workflow_marks_merged_clarification_and_hides_internal_sim_arg():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    merged = node_by_name(workflow, "Normalize Merged Clarification")["parameters"]["jsCode"]
    formatter = node_by_name(workflow, "Build vLLM User Response Format Body")["parameters"]["jsCode"]
    save_pending = node_by_name(workflow, "Save Pending Clarification Session State")["parameters"]["jsonBody"]

    assert "merge_clarification_called: true" in merged
    assert "merge_clarification_resolved: merge.resolved === true" in merged
    assert "merge_clarification_selected_option" in merged
    assert "merge_clarification_target_lines" in merged
    assert "Resolved clarification did not continue to REVIEWED candidate path" in merged
    assert "simulated tooling count" in formatter
    assert "operatorFacing" in formatter
    assert "simulated tooling count" in save_pending


def test_user_response_formatter_prompt_has_status_specific_instructions():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    code = node_by_name(workflow, "Build vLLM User Response Format Body")["parameters"]["jsCode"]

    for text in [
        "NEEDS_CLARIFICATION:",
        "REVIEWED:",
        "RELEASED:",
        "READY / DEGRADED:",
        "WAITING:",
        "GENERATED:",
        "REJECTED / NEEDS_REVISION:",
    ]:
        assert text in code
    assert "Do not decide workflow state." in code
    assert "Examples with valid JSON outputs matching the formatter schema:" in code
    assert "JSON.stringify(formatterExamples[0])" in code
    assert "The patch was released successfully, but the strategy cannot be switched immediately." in code
    assert "TRAY_COMPLETE checkpoint" in code


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


def test_system_evaluation_incomplete_message_is_deterministic_and_clears_stale_intent():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    formatter = node_by_name(workflow, "Normalize Formatted User Response")["parameters"]["jsCode"]
    scenario_context = node_by_name(
        workflow,
        "Normalize Context After ScenarioSpec Generation",
    )["parameters"]["jsCode"]

    assert "SYSTEM_EVALUATION_INCOMPLETE" in formatter
    assert "required_fields: []" in formatter
    assert "suggested_reply: ''" in formatter
    assert "pending_intent: null" in scenario_context


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
    create = node_by_name(workflow, "Create Reconciliation Plan")
    ready = node_by_name(workflow, "Return Ready Plan")
    body = create["parameters"]["jsonBody"]
    context_expr = assignment_value(ready, "context")

    assert "trt_id: $json.context?.trt_id || $json.payload?.trt_id || $json.trt_id || $json.body?.trt_id || null" in body
    assert "trt_version: $json.context?.trt_version || $json.payload?.trt_version || $json.trt_version || $json.body?.trt_version || null" in body
    assert "release_id: $json.context?.release_id || $json.payload?.release_id || null" in body
    assert "affected_lines: $json.context?.affected_lines || $json.payload?.affected_lines || []" in body
    assert "release_id: $('Receive Released TRT').first().json.context?.release_id || null" in context_expr
    assert "reconciliation_plan_id: $json.plan_id" in context_expr
    assert "trt_id: $('Receive Released TRT').first().json.context?.trt_id || $json.trt_id" in context_expr
    assert "trt_version: $json.trt_version || $('Receive Released TRT').first().json.context?.trt_version" in context_expr


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
    assert "scenario_template_id: context.scenario_template_id || payload.scenario_template_id || null" in code
    assert "surgical_sorting_4line_v1" not in code
    assert "include_waiting_scenarios: false" in code
    assert "context.affected_lines" in code
    assert "payload.affected_lines" in code
    assert "input.affected_lines" in code
    assert "const lineDecisions = payload.line_decisions || payload.plan?.line_decisions || []" in code
    assert "affected_lines: affectedLines" in code
    assert "line_decisions: lineDecisions" in code
    assert "const plan = payload.plan || {};" in code
    assert "ids.release_id" not in body


def test_generate_scenario_spec_runs_isaac_simulation_after_generation():
    workflow = load_workflow("generate_scenario_spec.workflow.json")
    run_node = node_by_name(workflow, "ScenarioSpec to Isaac Simulation Run")
    wait_node = node_by_name(workflow, "Wait Before Simulation Poll")
    poll_node = node_by_name(workflow, "Poll Isaac Simulation Run")
    terminal_node = node_by_name(workflow, "Simulation Run Terminal?")
    result_node = node_by_name(workflow, "Return Simulation Run Result")
    run_body = run_node["parameters"]["jsonBody"]
    status_expr = assignment_value(result_node, "status")

    assert run_node["type"] == "n8n-nodes-base.httpRequest"
    assert run_node["parameters"]["url"] == "http://trt-api:8000/simulation/runs"
    assert "scenario_spec_id: $json.scenario_spec_id" in run_body
    assert "scenario_spec_path:" in run_body
    assert "run_mode: 'ASYNC'" in run_body
    assert "headless: false" in run_body
    assert "line_" not in run_body
    assert workflow["connections"]["Generated?"]["main"][0][0]["node"] == "ScenarioSpec to Isaac Simulation Run"
    assert workflow["connections"]["ScenarioSpec to Isaac Simulation Run"]["main"][0][0]["node"] == "Wait Before Simulation Poll"
    assert wait_node["type"] == "n8n-nodes-base.wait"
    assert poll_node["parameters"]["url"] == "=http://trt-api:8000/simulation/runs/{{$json.run_id}}"
    assert workflow["connections"]["Wait Before Simulation Poll"]["main"][0][0]["node"] == "Poll Isaac Simulation Run"
    assert workflow["connections"]["Poll Isaac Simulation Run"]["main"][0][0]["node"] == "Simulation Run Terminal?"
    assert workflow["connections"]["Simulation Run Terminal?"]["main"][0][0]["node"] == "Return Simulation Run Result"
    assert workflow["connections"]["Simulation Run Terminal?"]["main"][1][0]["node"] == "Wait Before Simulation Poll"
    terminal_condition = terminal_node["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert "RUNNING" not in terminal_condition
    assert "COMPLETED" in terminal_condition
    assert "FAILED_TIMEOUT" in terminal_condition
    assert "SIMULATION_COMPLETED" not in status_expr
    assert "'GENERATED'" in status_expr
    assert "SIMULATION_FAILED" in status_expr
    assert "$json.error_code" in assignment_value(result_node, "errors")


def test_chat_scenario_summary_does_not_claim_generated_spec_missing_on_simulation_failure():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    summary_node = node_by_name(workflow, "Chat ScenarioSpec Path Summary")
    payload_expr = assignment_value(summary_node, "payload")

    assert "ScenarioSpec was generated, but simulation result processing failed" in payload_expr
    assert "$json.payload?.user_message || $json.payload?.message" in payload_expr
    assert "ScenarioSpec was generated and simulation completed" in payload_expr
    assert "SIMULATION_COMPLETED" not in payload_expr
    assert "ScenarioSpec was not generated" in payload_expr
    assert "scenario_spec_id" in payload_expr


def test_generate_scenario_spec_summarizes_evidence_after_simulation():
    workflow = load_workflow("generate_scenario_spec.workflow.json")
    evidence_node = node_by_name(workflow, "Summarize RunArtifact Evidence")
    build_node = node_by_name(workflow, "Build Evidence Summary Response")
    return_node = node_by_name(workflow, "Return Evidence Summary")

    assert evidence_node["parameters"]["url"] == "http://trt-api:8000/evidence/summarize"
    assert workflow["connections"]["Return Simulation Run Result"]["main"][0][0]["node"] == "Summarize RunArtifact Evidence"
    assert workflow["connections"]["Summarize RunArtifact Evidence"]["main"][0][0]["node"] == "Build Evidence Summary Response"
    assert workflow["connections"]["Build Evidence Summary Response"]["main"][0][0]["node"] == "Return Evidence Summary"
    assert "user_message" in build_node["parameters"]["jsCode"]
    assert "operator_explanation" in build_node["parameters"]["jsCode"]
    assert assignment_value(return_node, "status") == "={{ $json.status }}"
    payload_expr = assignment_value(return_node, "payload")
    assert payload_expr == "={{ $json.payload || {} }}"
    assert "=>" not in payload_expr
    assert "`" not in payload_expr


def test_milestone11_subworkflows_call_evidence_and_deployment_endpoints():
    evidence_workflow = load_workflow("run_artifact_to_evidence_summary.workflow.json")
    deploy_workflow = load_workflow("deployment_approval_demo.workflow.json")

    assert node_by_name(evidence_workflow, "Summarize RunArtifact Evidence")["parameters"]["url"] == "http://trt-api:8000/evidence/summarize"
    assert workflow_connection_target(evidence_workflow, "Summarize RunArtifact Evidence") == "Build Evidence Summary Response"
    assert workflow_connection_target(evidence_workflow, "Build Evidence Summary Response") == "Return Evidence Summary"
    assert assignment_value(node_by_name(evidence_workflow, "Return Evidence Summary"), "payload") == "={{ $json.payload || {} }}"
    assert node_by_name(deploy_workflow, "Simulated Physical Deployment")["parameters"]["url"] == "http://trt-api:8000/deployment/simulated-deploy"


def test_evidence_workflows_preserve_detailed_kpi_summary_before_deployment_prompt():
    evidence_workflow = load_workflow("run_artifact_to_evidence_summary.workflow.json")
    scenario_workflow = load_workflow("generate_scenario_spec.workflow.json")
    chat_workflow = load_workflow("chat_operator_task_allocation.workflow.json")

    evidence_code = node_by_name(evidence_workflow, "Build Evidence Summary Response")["parameters"]["jsCode"]
    scenario_code = node_by_name(scenario_workflow, "Build Evidence Summary Response")["parameters"]["jsCode"]
    formatter_code = node_by_name(chat_workflow, "Build vLLM User Response Format Body")["parameters"]["jsCode"]
    fallback_code = node_by_name(chat_workflow, "Normalize Formatted User Response")["parameters"]["jsCode"]

    assert "evidence.operator_detail_summary" in evidence_code
    assert "evidence.operator_detail_summary" in scenario_code
    assert "payload.evidence_summary.operator_detail_summary" in formatter_code
    assert "Do not use only payload.evidence_summary.operator_summary if detailed KPI rows exist" in formatter_code
    assert "canonical.payload?.evidence_summary?.operator_detail_summary" in fallback_code


def test_chat_classifier_supports_deployment_decision_turns():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    builder = node_by_name(workflow, "Build vLLM Dialogue Decision Body")
    normalizer = node_by_name(workflow, "Normalize Dialogue Decision")
    parser = node_by_name(workflow, "Parse Deployment Decision")
    code = builder["parameters"]["jsCode"] + normalizer["parameters"]["jsCode"] + parser["parameters"]["jsCode"]

    assert "DEPLOYMENT_DECISION" in code
    assert "WAITING_FOR_DEPLOYMENT_DECISION" in code
    assert "deploy now" in code
    assert "DO_NOT_DEPLOY" in code
    assert "RERUN_SIMULATION" in code


def test_chat_deployment_decision_routes_before_patch_review():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")
    loaded_gate = node_by_name(workflow, "Loaded Deployment Decision Pending?")
    direct_turn = node_by_name(workflow, "Build Direct Deployment Decision Turn")
    deployment_gate = node_by_name(workflow, "Deployment Decision Pending?")
    parser = node_by_name(workflow, "Parse Deployment Decision")
    deploy_call = node_by_name(workflow, "Call Simulated Deployment")
    deployment_context = node_by_name(workflow, "Validate Deployment Context")
    prompt_builder = node_by_name(workflow, "Build Pending Deployment Session State")
    save_pending = node_by_name(workflow, "Save Pending Deployment Session State")
    deployment_reply = node_by_name(workflow, "Ask Deployment Decision Reply")

    assert workflow["connections"]["Load Chat Session State"]["main"][0][0]["node"] == "Loaded Deployment Decision Pending?"
    assert workflow["connections"]["Loaded Deployment Decision Pending?"]["main"][0][0]["node"] == "Build Direct Deployment Decision Turn"
    assert workflow["connections"]["Loaded Deployment Decision Pending?"]["main"][1][0]["node"] == "Build vLLM Dialogue Decision Body"
    assert workflow["connections"]["Build Direct Deployment Decision Turn"]["main"][0][0]["node"] == "Parse Deployment Decision"
    assert workflow["connections"]["Normalize Dialogue Decision"]["main"][0][0]["node"] == "Deployment Decision Pending?"
    assert workflow["connections"]["Deployment Decision Pending?"]["main"][0][0]["node"] == "Parse Deployment Decision"
    assert workflow["connections"]["Deployment Decision Pending?"]["main"][1][0]["node"] == "Route Chat Turn"
    assert workflow["connections"]["Parse Deployment Decision"]["main"][0][0]["node"] == "Deployment Decision Is Deploy?"
    assert workflow["connections"]["Deployment Decision Is Deploy?"]["main"][0][0]["node"] == "Validate Deployment Context"
    assert workflow["connections"]["Validate Deployment Context"]["main"][0][0]["node"] == "Deployment Context Valid?"
    assert workflow["connections"]["Deployment Context Valid?"]["main"][0][0]["node"] == "Call Simulated Deployment"
    assert workflow["connections"]["Call Simulated Deployment"]["main"][0][0]["node"] == "Return Deployment Success"
    assert workflow["connections"]["Return Deployment Success"]["main"][0][0]["node"] == "Clear Deployment Session State"
    assert workflow["connections"]["Clear Deployment Session State"]["main"][0][0]["node"] == "Restore Deployment Success After Clear"
    assert workflow["connections"]["Restore Deployment Success After Clear"]["main"][0][0]["node"] == "Send Chat Response"
    assert workflow["connections"]["Evidence Requests Deployment Approval?"]["main"][0][0]["node"] == "Build Pending Deployment Session State"
    assert workflow["connections"]["Build Pending Deployment Session State"]["main"][0][0]["node"] == "Save Pending Deployment Session State"
    assert workflow["connections"]["Normalize Formatted User Response"]["main"][0][0]["node"] == "Formatted Response Needs Deployment Decision?"
    assert workflow["connections"]["Formatted Response Needs Deployment Decision?"]["main"][0][0]["node"] == "Ask Deployment Decision Reply"

    assert "WAITING_FOR_DEPLOYMENT_DECISION" in json.dumps(loaded_gate["parameters"])
    assert "DEPLOYMENT_DECISION" in direct_turn["parameters"]["jsCode"]
    assert "WAITING_FOR_DEPLOYMENT_DECISION" in json.dumps(deployment_gate["parameters"])
    assert "deployment_pending === true" in json.dumps(deployment_gate["parameters"])
    assert "TASK_REQUEST" not in parser["parameters"]["jsCode"]
    assert "deploy now" in parser["parameters"]["jsCode"]
    assert "Deployment cannot proceed because the pending deployment context is missing" in deployment_context["parameters"]["jsCode"]
    assert deploy_call["parameters"]["url"] == "http://trt-api:8000/deployment/simulated-deploy"
    assert "pending_deployment?.run_id" in deploy_call["parameters"]["jsonBody"]
    assert "Cannot save pending deployment state without real chat session_id" in prompt_builder["parameters"]["jsCode"]
    assert "WAITING_FOR_DEPLOYMENT_DECISION" in prompt_builder["parameters"]["jsCode"]
    assert "pending_deployment" in prompt_builder["parameters"]["jsCode"]
    assert save_pending["parameters"]["method"] == "PUT"
    assert save_pending["parameters"]["url"] == "=http://trt-api:8000/chat/session/{{$json.session_id}}"
    assert save_pending["parameters"]["jsonBody"] == "={{ $json.session_save_payload }}"
    assert deployment_reply["parameters"]["waitUserReply"] is True


def test_intent_prompt_does_not_hardcode_priority_clarification_scope():
    workflow = load_workflow("intent_to_patch_review.workflow.json")
    serialized = json.dumps(workflow)

    assert "robots on lines 1 and 3 pick ENT-required tools first" not in serialized
    assert "requested production-line scope" in serialized
    assert "Preserve ALL_LINES as all production lines" in serialized


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
    assert "payload: { ...payload, plan: normalizedPlan, affected_lines: affectedLines }" in code


def test_generate_scenario_spec_derives_affected_lines_from_candidate_patch_operations():
    workflow = load_workflow("generate_scenario_spec.workflow.json")
    code = node_by_name(workflow, "Validate ScenarioSpec Context")["parameters"]["jsCode"]

    assert "function affectedLinesFromOperations" in code
    assert "const path = operation.path || '';" in code
    assert "const lineIndex = parts.indexOf('lines');" in code
    assert "affected_lines: affectedLines" in code


def test_intent_review_derives_affected_lines_from_candidate_patch_operations():
    workflow = load_workflow("intent_to_patch_review.workflow.json")
    code = node_by_name(workflow, "Return Reviewed Candidate")["parameters"]["jsCode"]

    assert "function affectedLinesFromOperations" in code
    assert "const candidatePatch = $('Normalize Domain Candidate with Python').item.json.intent_patch" in code
    assert "affected_lines: affectedLines" in code
    for path_part in ["goal", "excluded_instruments", "abnormal_strategy"]:
        assert path_part not in code or "operation.path" in code


def test_chat_workflow_preserves_affected_lines_through_release_and_reconciliation():
    workflow = load_workflow("chat_operator_task_allocation.workflow.json")

    assert "affected_lines: $json.context.affected_lines || $json.payload.affected_lines || []" in assignment_value(
        node_by_name(workflow, "Chat Candidate Patch Summary"), "payload"
    )
    assert "affected_lines: affectedLines" in node_by_name(workflow, "Normalize Classified Approval Decision")[
        "parameters"
    ]["jsCode"]
    assert "firstArray(current.context?.affected_lines" in node_by_name(
        workflow, "Normalize Context After Release Approval"
    )["parameters"]["jsCode"]
    assert "firstArray(current.context?.affected_lines" in node_by_name(
        workflow, "Normalize Context After Reconciliation"
    )["parameters"]["jsCode"]


def test_release_approval_outputs_preserve_affected_lines():
    workflow = load_workflow("patch_release_approval.workflow.json")

    assert "function affectedLinesFromOperations" in node_by_name(workflow, "Normalize Release Approval Input")[
        "parameters"
    ]["jsCode"]
    assert "affected_lines: $('Normalize Release Approval Input').first().json.affected_lines || []" in assignment_value(
        node_by_name(workflow, "Return Pending Release"), "context"
    )
    assert "affected_lines: $('Normalize Release Approval Input').first().json.affected_lines || []" in assignment_value(
        node_by_name(workflow, "Released Notification Output"), "context"
    )
    for name in ["Rejected Notification Output", "Revision Notification Output"]:
        code = node_by_name(workflow, name)["parameters"]["jsCode"]
        assert "const affectedLines =" in code
        assert "affected_lines: affectedLines" in code


def test_reconciliation_outputs_preserve_affected_lines():
    workflow = load_workflow("released_trt_to_reconciliation.workflow.json")

    for name in ["Return Ready Plan", "Return Waiting Plan", "Return Degraded Plan", "Return Rejected Plan"]:
        assert "affected_lines: $('Receive Released TRT').first().json.context?.affected_lines" in assignment_value(
            node_by_name(workflow, name), "context"
        )
        assert "affected_lines: $('Receive Released TRT').first().json.context?.affected_lines" in assignment_value(
            node_by_name(workflow, name), "payload"
        )


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
