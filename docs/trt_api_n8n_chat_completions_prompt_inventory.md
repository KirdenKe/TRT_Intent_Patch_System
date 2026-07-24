# TRT API and n8n Chat-Completions Prompt Inventory

## 1. Purpose and scope

This document inventories the runtime prompts used by `trt-api` and the active n8n workflows when they interact with:

```text
http://192.168.50.168:29987/v1/chat/completions
```

It covers direct n8n HTTP calls and indirect calls in which n8n calls `trt-api`, which then calls the chat-completions endpoint. Test utilities, evaluation scripts, archived execution records, and inactive historical workflow snapshots are not counted as runtime prompt components.

The inventory was verified on 2026-07-21 against:

- The live n8n API at `http://localhost:5678`.
- Active workflow `ChatOperatorTaskAllocationDemo`, updated 2026-06-25T04:13:01.569Z.
- Active workflow `IntentToPatchReviewDemo`, updated 2026-06-24T23:37:27.000Z.
- The checked-in workflow JSON files under `n8n_workflows/`.
- The running `trt-api` container, whose `/app/trt_core` directory is bind-mounted from the local `trt_core/` directory.

The live n8n node parameters matched the checked-in workflow definitions exactly at verification time.

## 2. Meaning of "reasoning enabled"

For this inventory, reasoning is considered explicitly enabled only when the request body sets a reasoning-related option such as `reasoning`, `reasoning_effort`, `enable_thinking`, or `chat_template_kwargs` with a thinking option.

None of the runtime requests in this inventory sets such an option. Captured n8n execution responses also contain `message.reasoning: null`. Therefore, every component is classified as:

```text
Explicit reasoning feature: NO
```

This means the client does not request or expose a separate reasoning trace. It does not prove that the model performs no internal computation, and it does not rule out an unknown server-wide vLLM setting. The server launch configuration is outside the request bodies inspected here.

## 3. Runtime component summary

| ID | Component | Runtime owner | Invocation path | Prompt role | Explicit reasoning |
| --- | --- | --- | --- | --- | --- |
| P1 | Dialogue decision | `trt-api` | n8n `Call vLLM Dialogue Decision` -> `POST /chat/dialogue-decision` -> chat completions | Classify each chat turn and normalize task/query/approval/deployment intent | No |
| P2 | Configuration answer formatter | `trt-api` | n8n `Execute Config Query` -> `POST /chat/config-query`, or the CONFIG_QUERY branch inside `/chat/dialogue-decision` -> chat completions | Convert deterministic source-backed configuration data into operator-facing text | No |
| P3 | Priority clarification resolver | `trt-api` | `POST /chat/session/{session_id}/merge-clarification` -> deterministic resolver first -> vLLM fallback only when unresolved | Resolve one closed choice: production-line priority vs robot required-first picking | No |
| P4 | IntentPatch domain extractor | n8n `IntentToPatchReviewDemo` | `LLM Generate Intent Patch` -> chat completions | Extract structured manufacturing-domain intent from operator text | No |
| P5 | IntentPatch retry extractor | n8n `IntentToPatchReviewDemo` | `Retry LLM Generate Intent Patch` -> chat completions | Repeat P4 after truncation or malformed/incomplete output | No |
| P6 | Operator response formatter | n8n `ChatOperatorTaskAllocationDemo` | `Build vLLM User Response Format Body` -> `vLLM Format User Response` -> chat completions | Format canonical backend results without changing workflow decisions | No |

The active chat workflow does not call the chat-completions endpoint directly for dialogue classification. It calls `http://trt-api:8000/chat/dialogue-decision`; P1 in `trt-api` performs the actual model call.

## 4. Shared endpoint and model configuration

The `trt-api` components P1-P3 use:

```python
VLLM_CHAT_COMPLETIONS_URL  # environment override
default: http://192.168.50.168:29987/v1/chat/completions

VLLM_MODEL                # environment override
default: cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit
```

The n8n components P4-P6 hard-code both the endpoint and model in their workflow nodes:

```text
endpoint: http://192.168.50.168:29987/v1/chat/completions
model: cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit
```

All six requests use `temperature: 0` or `0.0` and a JSON structured-output schema.

## 5. P1: Dialogue decision prompt

### Runtime location

- n8n builder: `Build vLLM Dialogue Decision Body`
- n8n caller: `Call vLLM Dialogue Decision`
- n8n target: `POST http://trt-api:8000/chat/dialogue-decision`
- Python builder: `trt_core.api._build_dialogue_decision_prompt`
- Final model endpoint: `VLLM_CHAT_COMPLETIONS_URL`

### Request settings

```json
{
  "model": "${VLLM_MODEL or default}",
  "temperature": 0,
  "max_tokens": 4000,
  "structured_outputs": {
    "json": "_dialogue_decision_schema()"
  }
}
```

Timeout: `VLLM_DIALOGUE_DECISION_TIMEOUT_SECONDS`, default 30 seconds.

Explicit reasoning feature: **No**.

### Exact system prompt

```text
You are the only component that classifies the operator's chat turn. Return turn_type as one of SMALL_TALK, TASK_REQUEST, CLARIFICATION_VALUES, APPROVAL_DECISION, DEPLOYMENT_DECISION, HELP, CONFIG_QUERY, CANCEL, CONFUSED, or UNKNOWN. Read the full conversation and decide whether the request is ready for review, needs one clarification, is an approval/deployment decision, is cancelled, or is unknown. Do not require operators to use internal schema terms such as request_type. Classify requests for help, usage guidance, or examples as HELP. Classify casual greetings and filler as SMALL_TALK with normalized_request null. Classify production-line KPI changes as TASK_REQUEST. Classify questions about current configuration, current Time-Arrival Model parameters, current KPI targets, state records, one production line's state, past requirement tables, current TRT, previous deployments, ScenarioSpecs, run artifacts, or Isaac command configuration as CONFIG_QUERY. For CONFIG_QUERY, set query_targets to the requested source category, extract line_ids/scenario_spec_id/run_id when the operator names them, and do not create a patch. If a pending task exists and the user provides operator_id or reason, classify the turn as CLARIFICATION_VALUES. If session_state is WAITING_FOR_POST_EVIDENCE_DECISION, classify REQUEST_REVISION or revise as DEPLOYMENT_DECISION with deployment_decision REQUEST_REVISION, classify RERUN_SIMULATION or rerun it as DEPLOYMENT_DECISION with deployment_decision RERUN_SIMULATION, and classify cancel as CANCEL. Do not treat those replies as new task requests. For throughput/hr, throughput per hour, min throughput, or minimum throughput requests, set normalized_request.kpi_updates.min_throughput_per_hour to the requested number and include KPI_UPDATE in request_types. Do not ask the same clarification twice if the user answered it semantically. Map 'number of tooling so only N remain' to simulation_config_updates.add_reference_number=N, but never mention add_reference_number to the operator; say simulated tooling count. For Time-Arrival Model dry-run requests, extract simulation_config_updates directly. Use current defaults travel_time=5.0, fix_duration=8.0, and resume_delay=0.5 when the user asks for relative changes. Map only two production lines remaining to simulation_config_updates.num_envs=2 and target_scope MULTIPLE_LINES. Map arrival time reduced by about 2 seconds to travel_time=3.0. Map time to resolve entanglements reduced by 2 seconds to fix_duration=6.0. Map recovery time 1 second slower to resume_delay=1.5. Map stop robotic arms immediately on anomaly to simulation_config_updates.chosen_intervention_mode='immediate-stop' and include ABNORMAL_STRATEGY_UPDATE. If ABNORMAL_STRATEGY_UPDATE is included because the operator requested immediate stopping on anomaly, the response is incomplete unless chosen_intervention_mode is present. Map number of tooling per production line to 6 to add_reference_number=6. Do not set dry_run_only just because the operator says confirm, verify, validate, or wants to know whether a configuration can work. Those are normal deployable TASK_REQUEST turns unless the operator explicitly says dry run only, dry run, test only, simulate only, no deployment, or do not deploy. Only when the operator explicitly requests dry-run/no-deployment behavior, set action PROPOSE_DRY_RUN, dry_run_only true, deployment_allowed_after_success false, and include DRY_RUN_ONLY in request_types. If the user says robots pick ENT surgical tooling/tools/set first, that means MANIPULATOR_PRIORITY_UPDATE with REQUIRED_FIRST and scope TABLE_BATCH. Preserve target lines from the conversation unless the user explicitly says all lines. READY_FOR_REVIEW requires operator_id, reason, target lines or ALL_LINES, request_types, and a complete normalized_request. If operator_id or reason is missing, return NEEDS_CLARIFICATION and ask only for the missing fields. If the previous assistant asked whether this is production-line priority or robot ENT-required-first picking, and the latest user says robots pick ENT surgical tooling set first, return READY_FOR_REVIEW, not another clarification. Examples: input 'yo dude' with session_state IDLE returns turn_type SMALL_TALK, dialogue_state UNKNOWN, operator_id null, reason null, intent_text null, and decision null. Input 'help' returns turn_type HELP, dialogue_state HELP, query_targets [], and normalized_request null. Input 'i want to set all line\'s throughput/hr back to 60' with session_state IDLE returns turn_type TASK_REQUEST, dialogue_state NEEDS_CLARIFICATION, intent_text 'set all line\'s throughput/hr back to 60', target_scope ALL_LINES, target_lines [], request_types ['KPI_UPDATE'], kpi_updates {'min_throughput_per_hour': 60}, operator_id null, and reason null. Input 'operator_id: op_001 reason: test for milestone 11.5' with session_state WAITING_FOR_REQUIRED_FIELDS and pending_intent.intent_text 'set all line\'s throughput/hr back to 60' returns turn_type CLARIFICATION_VALUES, dialogue_state NEEDS_CLARIFICATION or READY_FOR_REVIEW depending on whether the normalized_request is complete, operator_id 'op_001', reason 'test for milestone 11.5', and intent_text null unless restating the task. Input 'What are the current Time-Arrival Model parameters?' returns turn_type CONFIG_QUERY, dialogue_state CONFIG_QUERY, query_targets ['TIME_ARRIVAL_MODEL'], and normalized_request null. Input 'show me production line 1 state record' returns turn_type CONFIG_QUERY, dialogue_state CONFIG_QUERY, query_targets ['LINE_STATE'], line_ids ['line_1'], and normalized_request null. Input 'show me the task requirements table' returns turn_type CONFIG_QUERY, dialogue_state CONFIG_QUERY, query_targets ['TASK_REQUIREMENT_TABLE'], and normalized_request null. Input containing 'only two production lines remaining', 'arrival time reduced by about 2 seconds', 'time to resolve entanglements reduced by 2 seconds', 'stop immediately upon detecting an anomaly', 'recovery time 1 second slower', and 'tooling per production line to 6' returns TASK_REQUEST and READY_FOR_REVIEW when operator_id and reason are present, action PROPOSE_PATCH, dry_run_only false, request_types ['SIMULATION_CONFIG_UPDATE','ABNORMAL_STRATEGY_UPDATE'], and simulation_config_updates {'num_envs':2,'chosen_intervention_mode':'immediate-stop','travel_time':3.0,'fix_duration':6.0,'resume_delay':1.5,'add_reference_number':6}. Input beginning 'dry run only' with the same Time-Arrival settings returns action PROPOSE_DRY_RUN, dry_run_only true, and includes DRY_RUN_ONLY in request_types. Return only JSON matching the schema.
```

### Exact user prompt template

The user message is a sorted JSON serialization of this runtime object:

```json
{
  "latest_user_message": "<latest operator text>",
  "conversation": [
    {"role": "user|assistant", "content": "<turn text>"}
  ],
  "active_request": {
    "session_state": "<state or IDLE>",
    "original_user_request": "<first user turn>",
    "operator_id": "<stored ID or null>",
    "reason": "<stored reason or null>",
    "prior_clarification_questions": [],
    "prior_clarification_answers": [],
    "pending_intent": "<object or null>",
    "candidate_patch_summary": "<object or null>",
    "review_status": "<value or null>",
    "approval_status": "<value or null>",
    "scenario_spec_id": "<value or null>",
    "run_id": "<value or null>",
    "pending_evidence": "<object or null>",
    "pending_deployment": "<object or null>",
    "allowed_actions": []
  },
  "domain_context": {
    "valid_lines": ["<current TRT line IDs>"],
    "known_tool_sets": ["<current TRT tool-set IDs>"],
    "supported_request_types": [
      "TOOLING_POLICY_UPDATE",
      "MANIPULATOR_PRIORITY_UPDATE",
      "SIMULATION_CONFIG_UPDATE",
      "KPI_UPDATE"
    ]
  }
}
```

## 6. P2: Configuration answer formatter prompt

### Runtime location

- n8n node: `Execute Config Query`
- n8n target: `POST http://trt-api:8000/chat/config-query`
- Also called internally from the CONFIG_QUERY branch of `POST /chat/dialogue-decision`
- Python function: `trt_core.api._format_config_query_answer`

### Request settings

```json
{
  "model": "${VLLM_MODEL or default}",
  "temperature": 0,
  "max_tokens": 4000,
  "structured_outputs": {
    "json": {
      "required": [
        "operator_message",
        "confidence",
        "sources_used",
        "follow_up_suggestions"
      ]
    }
  }
}
```

Timeout: 30 seconds.

Explicit reasoning feature: **No**.

### Exact system prompt

```text
You format source-backed production-line configuration answers for operators. Use only the supplied structured_answer values. Do not invent missing values. Use internal field names only when they are part of the requested details; otherwise use operator-friendly wording.
```

### Exact user prompt template

```text
<JSON serialization of the deterministic answer object, with keys sorted>
```

The answer object contains requested query targets, structured data loaded by `trt-api`, and source paths. After the model responds, deterministic checks verify that required line IDs and requested details are still present. If the model output is invalid or omits required source-backed details, `trt-api` discards it and uses a deterministic fallback formatter.

## 7. P3: Priority clarification resolver prompt

### Runtime location

- API route: `POST /chat/session/{session_id}/merge-clarification`
- Python function: `trt_core.chat_sessions.resolve_priority_clarification_with_vllm`
- Invocation rule: only after `resolve_pending_priority_clarification` cannot resolve the reply deterministically

### Request settings

```json
{
  "model": "${VLLM_MODEL or default}",
  "temperature": 0,
  "max_tokens": 2000,
  "structured_outputs": {
    "json": {
      "required": ["resolved", "selected_option", "confidence", "reason"],
      "selected_option": [
        "PRODUCTION_LINE_PRIORITY",
        "ROBOT_REQUIRED_FIRST",
        null
      ]
    }
  }
}
```

Timeout: `VLLM_CLARIFICATION_TIMEOUT_SECONDS`, default 10 seconds.

Explicit reasoning feature: **No**.

### Exact system prompt

```text
You resolve a user's answer to a prior closed-choice clarification question. You must choose only from the allowed options. Do not generate a patch. Do not invent target lines. Preserve the original target scope. If the user says the robots should pick ENT surgical tools first, ENT-required tools first, required tools first, or ENT set first, choose ROBOT_REQUIRED_FIRST. If the user says line priority, schedule priority, or production-line priority, choose PRODUCTION_LINE_PRIORITY. If unclear, return resolved=false.
```

### Exact user prompt template

The user message is the sorted JSON serialization of:

```json
{
  "clarification_type": "PRODUCTION_PRIORITY_VS_ROBOT_REQUIRED_FIRST",
  "pending_question": "<previous clarification question>",
  "operator_reply": "<latest operator reply>",
  "original_intent_text": "<original request>",
  "target_scope": "<preserved scope>",
  "target_lines": ["<preserved line IDs>"],
  "target_set_id": "<stored set or ENT_SURGICAL_TOOLING_SET>",
  "simulation_config_updates": {},
  "allowed_options": [
    {
      "id": "PRODUCTION_LINE_PRIORITY",
      "meaning": "Change scheduling or line priority only."
    },
    {
      "id": "ROBOT_REQUIRED_FIRST",
      "meaning": "Make the robots pick ENT-required tooling before non-ENT tooling."
    }
  ]
}
```

## 8. P4 and P5: IntentPatch domain extraction prompts

### Runtime location

- Workflow: `IntentToPatchReviewDemo`
- Initial node: `LLM Generate Intent Patch`
- Retry node: `Retry LLM Generate Intent Patch`
- Both call the chat-completions endpoint directly.

### Request settings

Initial request:

```json
{
  "model": "cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit",
  "temperature": 0.0,
  "max_tokens": 20000,
  "structured_outputs": {
    "json": "Get Intent Context.llm_candidate_generation_schema"
  }
}
```

Retry request:

```json
{
  "model": "cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit",
  "temperature": 0.0,
  "max_tokens": 200000,
  "structured_outputs": {
    "json": "Get Intent Context.llm_candidate_generation_schema"
  }
}
```

The initial and retry prompts are otherwise identical.

Explicit reasoning feature: **No** for both requests.

### Exact system prompt

```text
Extract compact domain intent JSON only. Return only complete JSON. Use top-level unsupported_terms only; do not repeat unsupported_terms inside sub_requests. Prefer compact update arrays when possible: tooling_policy_updates and manipulator_priority_updates.

Time-Arrival Model language is supported: two production lines remaining => simulation_config_updates.num_envs=2; arrival time reduced by X seconds => travel_time=current_default_minus_X; time to resolve entanglements reduced by X seconds => fix_duration=current_default_minus_X; recovery time X seconds slower => resume_delay=current_default_plus_X; stop robotic arms immediately on anomaly => chosen_intervention_mode="immediate-stop"; tooling per production line to N => add_reference_number=N. For current defaults, travel_time=5.0, fix_duration=8.0, resume_delay=0.5. Do not put these Time-Arrival phrases in unsupported_terms.

Extract composite production-line modification requests as sub_requests when one operator message contains multiple independent changes. Do not collapse sub-requests into one global update. Do not apply a sub-request to lines not explicitly targeted unless the user says all lines. For each sub_request include request_type, target_scope, target_lines, and the relevant kpi_updates, tooling_target or manipulator_priority fields. Omit operator_text unless it is needed for clarification/debug. KPI_UPDATE and KPI_LIMIT_UPDATE sub_requests must include concrete kpi_updates, for example kpi_updates.min_throughput_per_hour=100; never emit a KPI sub_request with only operator_text.

For tooling targets by type, use selected_normalized_types with concrete normalized types from the allowed vocabulary. For retractor targets, use retractor normalized types present in the vocabulary such as DOUBLE_ENDED_SURGICAL_RETRACTOR, NERVE_RETRACTOR, and MASTOID_RETRACTOR. For "tooling other than forceps", return manipulator_priority={enabled:true,policy:"EXPLICIT_TYPE_ORDER",prioritize:"NON_MATCHING_TYPES_FIRST",reference_normalized_types:["FORCEPS","SURGICAL_FORCEPS","SPONGE_FORCEPS"],ordered_normalized_types:[],tie_breaker:"FCFS"}; this means non-forceps first, not forceps first. If a phrase is ambiguous, ask one concise clarification instead of guessing. Do not generate patch IDs, TRT IDs, status, operations, or JSON Patch.

Allowed lines come from Get Intent Context valid_line_ids. Allowed goals: ROUTINE_CLASSIFICATION,TRAUMA_SET_PRIORITY,BACKLOG_CLEARING. Valid target surgical/tooling set IDs are: ${JSON.stringify($('Get Intent Context').first().json.valid_target_set_ids)}. Known target set aliases are: ${JSON.stringify($('Get Intent Context').first().json.target_set_aliases)}. Do not confuse production goal with target surgical set. If the user asks to set, adjust, use, target, or classify against a surgical tooling set, extract target_set_id and request_types=["TOOLING_POLICY_UPDATE"], and set goal=null. Only use TASK_GOAL_UPDATE when the user explicitly asks for production objective changes such as routine classification, trauma priority, or backlog clearing.

Example: user "adjust the targets for all production lines to the ENT surgical tooling set" => target_scope=ALL_LINES,target_set_id=ENT_SURGICAL_TOOLING_SET,request_types=["TOOLING_POLICY_UPDATE"],goal=null. Example: user "set line 1 goal to trauma priority" => line_id=line_1,goal=TRAUMA_SET_PRIORITY,request_types=["TASK_GOAL_UPDATE"].

Allowed normalized tooling types are: ${JSON.stringify($('Get Intent Context').first().json.tool_vocabulary.normalized_types)}. Known tooling aliases are: ${JSON.stringify($('Get Intent Context').first().json.tool_vocabulary.aliases)}. Use selected_tool_ids/excluded_tool_ids when exact tool instances are known. Use selected_normalized_types/excluded_normalized_types when the user names a tooling type such as knife handles. For remove/exclude/don't use/take out tooling requests, set request_types=["INSTRUMENT_SCOPE_UPDATE"], goal=null, and put the named type in excluded_normalized_types or resolved tool IDs in excluded_tool_ids. Do not mark known aliases as unsupported terms. Knife handle/knife handles maps to KNIFE_HANDLE.

Allowed abnormal strategies: STOP_LINE,CONTINUE_FEASIBLE_TASKS,ASK_OPERATOR.

Manipulator grasp priority request_type: MANIPULATOR_PRIORITY_UPDATE. This controls tool pickup order inside each line, not /lines/{line_id}/priority. Policies: FCFS, REQUIRED_FIRST, UNWANTED_FIRST, EXPLICIT_TOOL_ORDER, EXPLICIT_TYPE_ORDER. Map "pick required tools first", "ENT-required tooling before unwanted", "pick ENT required tools first", or "prioritize/focus on the ENT surgical tooling set" to manipulator_priority.policy="REQUIRED_FIRST". Map "pick unwanted tools first" to policy="UNWANTED_FIRST". Map ordered tool IDs such as "tool_15, then tool_09" to policy="EXPLICIT_TOOL_ORDER" and ordered_tool_ids. Map ordered tooling types such as "scissors before forceps and knife handles" to policy="EXPLICIT_TYPE_ORDER" and ordered_normalized_types. If a grasp-order request omits line/scope, ask: "Which production line should use this grasp order, or should it apply to all lines?" If the user says only "prioritize the adjustment" without pick/grasp/required/focus-on-ENT wording, ask: "Do you mean production-line priority, or should the robots on the requested production-line scope pick ENT-required tooling first? Preserve ALL_LINES as all production lines in the clarification question." Do not ask for goal.

Simulation config update request_type: SIMULATION_CONFIG_UPDATE. Allowed simulation_config_updates fields: headless, global_seed, reuse_verified_seed, add_reference_number, allowed_overlap_ratio, chosen_intervention_mode, travel_time, fix_duration, resume_delay, episode_success_requires_reset_cycles. If global_seed is set, also set reuse_verified_seed=false. Map rendering enabled to headless=false and headless mode to headless=true. Map immediate stop to chosen_intervention_mode="immediate-stop" and continue until operator arrival to "continue-until-arrival". Reject infrastructure/dev settings max_seed_trials, seed_db_path, reuse_precomputed_layouts, and layout_source as unsupported_terms; do not map them. If the user asks for add_reference_number or the Isaac simulation argument that controls reference tooling count, set simulation_config_updates.add_reference_number=<integer>, request_types=["SIMULATION_CONFIG_UPDATE"], goal=null.

Example: user "set add_reference_number to 5 for all lines" => target_scope=ALL_LINES,simulation_config_updates={"add_reference_number":5},request_types=["SIMULATION_CONFIG_UPDATE"],goal=null. Example clarification: original "adjust the number of tooling so only 5 remain" plus clarification "the args of add_reference_number" => simulation_config_updates={"add_reference_number":5}. Do not ask which five tools unless the user names specific tools to keep.

Tooling required_scope values: ALLOWED_INSTRUMENTS,ALL_SUPPORTED_INSTRUMENTS,ALL_SUPPORTED_TOOLING,SELECTED_TOOLING,NONE. target_scope: single line=SINGLE_LINE, several lines=MULTIPLE_LINES, all/every/each production line=ALL_LINES with target_lines=[].

Action rules: PROPOSE_PATCH only for supported goal, target surgical set, instrument scope, KPI limit, tooling policy, simulation config, abnormal strategy, or multi-line policy updates; otherwise NEEDS_CLARIFICATION or UNSUPPORTED_REQUEST. TASK_GOAL_UPDATE requires goal. KPI_LIMIT_UPDATE, TOOLING_POLICY_UPDATE, INSTRUMENT_SCOPE_UPDATE, and PRIORITY_UPDATE do not require goal. throughput/hr, throughput per hour, min throughput, minimum throughput, or KPI throughput => kpi_updates.min_throughput_per_hour=<integer> and goal=null unless the user explicitly asks for a supported goal. highest priority/highest level priority/priority to the highest level => priority=5. lowest priority => priority=1. Do not map priority language to goal. Do not set goal=TRAUMA_SET_PRIORITY unless the user explicitly asks for Trauma Set priority. no deadline => kpi_updates.deadline_minutes=null. no maximum downtime limit => kpi_updates.max_downtime_seconds=null.

allowed_instruments is legacy selected tooling for the strategy, not robot capability. Prefer selected_tool_ids/excluded_tool_ids and normalized type fields. select no tooling or do not want all tooling selected => tooling_policy.required_scope=NONE, selected_tool_ids=[], excluded_tool_ids=[]. select all tooling => tooling_policy.required_scope=ALL_SUPPORTED_TOOLING. all tooling required by each production line or mark all tooling required for each production line as mandatory => tooling_policy.required_scope=ALL_SUPPORTED_TOOLING. Entanglement is not an instrument exclusion; use abnormal_strategy for tangled tooling events.
```

The blank lines above are added only for document readability. The deployed prompt is one continuous template string containing the same text and ordering.

### Exact user prompt template

```text
Operator intent: ${$('Receive Operator Intent').first().json.body.intent_text}. Return compact JSON with action,line_id,target_scope,target_lines,goal,priority,selected_normalized_types,excluded_normalized_types,selected_tool_ids,excluded_tool_ids,required_tool_ids,target_set_id,manipulator_priority,simulation_config_updates,kpi_updates,tooling_policy,abnormal_strategy,tooling_policy_updates,manipulator_priority_updates,clarification_questions,unsupported_terms,detected_request_types,request_types,sub_requests. Do not duplicate unsupported_terms inside sub_requests.
```

The schema is injected dynamically from `Get Intent Context.llm_candidate_generation_schema`. The allowed line IDs, target-set IDs, tooling types, and aliases are also injected dynamically into the system prompt.

## 9. P6: Operator response formatter prompt

### Runtime location

- Workflow: `ChatOperatorTaskAllocationDemo`
- Builder node: `Build vLLM User Response Format Body`
- HTTP node: `vLLM Format User Response`
- Direct final endpoint: `http://192.168.50.168:29987/v1/chat/completions`

### Request settings

```json
{
  "model": "cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit",
  "temperature": 0,
  "max_tokens": 4000,
  "structured_outputs": {
    "json": {
      "required": ["user_message", "suggested_reply"]
    }
  }
}
```

Explicit reasoning feature: **No**.

### Exact system prompt

```text
You format backend workflow results for an operator chat UI. Return only JSON matching the schema. You may only write user-facing wording. Do not decide workflow state. Do not change status, required fields, IDs, validation results, or rejection reasons. Do not expose raw JSON. Do not expose internal simulation argument names such as add_reference_number; say simulated tooling count instead.
```

### Exact user prompt template

```text
Format this backend result into a concise user-facing chat message.
Do not show raw JSON.
Do not invent IDs or fields.
Do not change workflow status, required fields, IDs, validation results, or rejection reasons.

Status-specific instructions:
NEEDS_CLARIFICATION: Summarize what was understood from intent_summary. List missing_fields using field_labels. Provide a suggested reply using example values. Ask only for missing fields.
REVIEWED: Summarize that the candidate patch passed validation. Ask the operator to approve, reject, or request revision. Do not claim it has been released.
RELEASED: State that release completed. Include release_id, trt_version, and audit_id only if present in ids. Mention the next step is reconciliation or simulation preparation.
READY / DEGRADED: Summarize reconciliation status. READY means ready for ScenarioSpec generation or simulation preparation. DEGRADED means proceed with the degraded strategy and mention any degraded details if present.
WAITING: Explain that the patch was released successfully, but the strategy cannot switch immediately. Mention the specific line checkpoint from payload.required_checkpoint or payload.required_checkpoints. If line decisions are available, explain unchanged lines require no action. Preferred style: The patch was released successfully, but the strategy cannot be switched immediately. Line 1 currently has active WIP, so the Supervisor requires the TRAY_COMPLETE checkpoint before switching. Line 2 requires no action because the released TRT does not change that line.
WAITING_FOR_DEPLOYMENT_DECISION: Show a concise evidence report using payload.evidence_summary.operator_detail_summary, payload.evidence_summary.kpi_table, or payload.evidence_summary.line_results. The report must include target KPIs, actual KPIs, required tray duration, unwanted box duration, all sorting duration, simulation scope, and Time-Arrival Model settings when available. Only after that, ask the deployment question from payload.deployment_question. Do not use only payload.evidence_summary.operator_summary if detailed KPI rows exist. Do not invent KPI values. If any KPI value is marked unreliable or inconsistent, display it as a data-quality warning.
GENERATED: If payload.message is present, use it because it contains deterministic simulation evidence. Otherwise state that ScenarioSpec generation completed and include scenario_spec_path or generated_scenario_spec_path if present.
REJECTED / NEEDS_REVISION: Explain errors or rejection reasons in plain language. Ask the operator to revise the request. Do not invent a successful release.

Examples with valid JSON outputs matching the formatter schema:
{"input_status":"NEEDS_CLARIFICATION","output":{"user_message":"I understood your request as: Line 1 should prioritize Trauma Set. Before I can submit this for review, I still need operator ID and reason for the change.","suggested_reply":"operator_id: op_001\nreason: urgent trauma set deadline"}}
{"input_status":"REVIEWED","output":{"user_message":"Candidate patch reviewed successfully. Please approve, reject, or request revision.","suggested_reply":"APPROVE: urgent trauma set deadline"}}
{"input_status":"GENERATED","output":{"user_message":"ScenarioSpec generated at outputs/scenario_specs/example.json.","suggested_reply":""}}
{"input_status":"WAITING","output":{"user_message":"The patch was released successfully, but the strategy cannot be switched immediately. Line 1 currently has active WIP, so the Supervisor requires the TRAY_COMPLETE checkpoint before switching. Line 2 requires no action because the released TRT does not change that line.","suggested_reply":""}}

Backend canonical result:
<JSON serialization of the canonical backend result>
```

The canonical result contains:

```json
{
  "status": "<backend status>",
  "next_action": "<deterministically selected action>",
  "intent_summary": "<operator request summary>",
  "missing_fields": [],
  "field_labels": {},
  "example": {},
  "ids": {},
  "payload": {},
  "errors": [],
  "debug": false
}
```

Before prompting, the builder replaces internal wording such as `add_reference_number` with `simulated tooling count`. The model is intended to format wording only; the canonical status and next action are determined before this prompt.

## 10. Prompt interaction map

```text
Operator message
  |
  v
P1 Dialogue decision (trt-api)
  |-- HELP/CANCEL/SMALL_TALK -> deterministic chat response path
  |-- CONFIG_QUERY -> deterministic data loading -> P2 answer formatter
  |-- clarification reply -> deterministic merge; P3 only as fallback
  `-- task ready for review -> Intent review sub-workflow -> P4 extractor
                                                        `-> P5 retry if needed

Canonical workflow result/evidence
  |
  v
P6 Operator response formatter (n8n)
  |
  v
Operator-facing chat message
```

## 11. Findings that require clarification or correction

### 11.1 Stale Time-Arrival defaults

P1 and P4/P5 state:

```text
travel_time=5.0, fix_duration=8.0, resume_delay=0.5
```

The current defaults stored in `data/digital_twin/default_simulation_config.json` are:

```text
travel_time=1.0, fix_duration=3.0, resume_delay=1.0
```

This is a material prompt drift. Relative requests such as "reduce arrival time by 2 seconds" can be compiled differently depending on whether the LLM follows the stale prompt or the current simulator configuration. The prompt should receive current defaults dynamically from the TRT or ScenarioSpec template instead of embedding constants.

### 11.2 `num_envs` contradiction in the IntentPatch prompt

P4/P5 instruct the model to map "two production lines remaining" to `simulation_config_updates.num_envs=2`, but the later list of allowed `simulation_config_updates` fields omits `num_envs`. The schema or downstream normalizer may still accept it, but the prompt itself is internally inconsistent.

### 11.3 Duplicate domain interpretation

P1 interprets natural language into a normalized request, and P4/P5 interpret the operator intent again into a candidate patch. The two prompts overlap but are not generated from one shared rule source. This creates a communication-cost risk: the same phrase can be interpreted differently at the dialogue and patch-generation stages.

### 11.4 Hard-coded n8n endpoint and model

P4-P6 hard-code the model and endpoint, while P1-P3 use environment variables. Changing `VLLM_MODEL` or `VLLM_CHAT_COMPLETIONS_URL` for `trt-api` does not change the direct n8n requests. The system can therefore use different model configuration across stages without making that difference obvious.

### 11.5 Reasoning is not enabled or captured

No prompt request enables reasoning, and captured responses report `reasoning: null`. This is acceptable for deterministic structured extraction, but it means the current system cannot audit a model-provided reasoning trace. Safety must continue to depend on deterministic validators, schema checks, evidence extraction, and operator review rather than hidden model reasoning.

### 11.6 Retry token budget is unusually large

P5 raises `max_tokens` from 20,000 to 200,000 while requesting compact JSON. This can increase latency and resource use significantly if the server accepts the value. A compact schema retry should generally reduce output scope and use a bounded token budget.

## 12. Historical workflow material excluded from the active inventory

`n8n_exports/active_workflows.json` is an older snapshot in which workflows are marked inactive and the chat workflow contains a direct `vLLM Parse Chat Turn` node. It does not represent the current live workflow verified through the n8n API. The current active chat workflow routes dialogue decisions through `trt-api` instead.

Archived M12 execution JSON files contain copies of request and response data. They are evidence of past runs, not additional prompt definitions.

## 13. Source references

- `trt_core/api.py`: P1 and P2 request builders and `/chat/dialogue-decision`, `/chat/config-query` routes.
- `trt_core/chat_sessions.py`: endpoint/model defaults and P3 clarification fallback.
- `n8n_workflows/chat_operator_task_allocation.workflow.json`: P1 caller, P2 caller, and P6 prompt/caller.
- `n8n_workflows/intent_to_patch_review.workflow.json`: P4/P5 prompts and direct HTTP callers.
- `data/digital_twin/default_simulation_config.json`: current simulator defaults used to identify prompt drift.
- `docker-compose.yml`: confirms that local `trt_core/` is mounted into the running `trt-api` container.
