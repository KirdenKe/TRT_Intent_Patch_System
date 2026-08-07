# Research Capability Audit: Pre-Improvement Baseline

> **Status:** Historical baseline. This audit describes the repository before
> the multi-candidate implementation added on 2026-07-29. For the current
> architecture, candidate objective, experimental protocol, and limitations,
> see `docs/system_improvement_and_experiment_specification.md`.

## 1. Purpose and audit basis

This document audited what the repository implemented before the 2026-07-29
multi-candidate change. It is retained to make the capability transition
traceable and must not be cited as the current runtime design.

The audit was conducted by reading:

- `trt_core/`, `scenario_generation/`, `schemas/`, and the n8n workflow JSON.
- Unit tests for patching, reconciliation, ScenarioSpec generation, the digital
  twin adapter, evidence extraction, deployment, and n8n routing.
- The M12 manual test packet, the 27-item reviewed trial, the archived full-run
  results, rescoring outputs, execution snapshots, chat sessions, generated
  reconciliation plans, and generated ScenarioSpecs.
- `outputs/reports/m12/n8n_access_report.json`, which recorded an accessible
  active chat workflow on 2026-07-06.
- `docs/trt_api_n8n_chat_completions_prompt_inventory.md`, whose live n8n
  verification was performed on 2026-07-21.

No workflow, application code, test input, configuration, TRT, or result data
was changed during the baseline audit. The repository was changed afterward.

## 1.1 Current capability delta

The remainder of this document preserves the pre-improvement evidence. The
current implementation differs in these specific, verified ways:

| Capability | Historical baseline | Current implementation |
| --- | --- | --- |
| Candidate strategies | One trace-labelled `primary` ScenarioSpec | LLM-generated batch of 2-8 schema-valid, behaviorally distinct candidates |
| State context | Reconciliation plan only | Released TRT, reconciliation decisions, aligned line-state snapshot, and persisted Time-Arrival state |
| Constraint preservation | One approved patch | Approved release operations lock candidate line-policy fields; KPI, tooling, task, scope, and Time-Arrival constraints cannot be varied |
| Digital-twin execution | One ScenarioSpec | One ScenarioSpec and one Isaac run per candidate, under a single-process sequential execution lock |
| Selection | No cross-strategy ranking | Hard physical/evidence gates followed by throughput-attainment ranking and deterministic tie-breakers |
| Failed candidate output | Intent extraction retry only | Candidate batch output is regenerated after deterministic format, schema, constraint, or distinctness failure, up to a bounded attempt limit |
| Deployment guard | Evidence check for one scenario | Evidence check plus proof that the requested ScenarioSpec/RunArtifact is the selected batch winner |
| Production scheduling | Not implemented | Still not implemented; candidate policy selection is not job-shop scheduling or cross-line task allocation |
| Physical deployment | Not implemented | Still not implemented; the endpoint remains a simulated deployment/state update |

The active n8n workflow `GenerateScenarioSpecDemo` was updated and verified
active on 2026-07-29. The current design and experimental protocol are defined
in `docs/system_improvement_and_experiment_specification.md`.

## 2. Executive conclusion

The implemented system is best described as:

> A human-gated natural-language-to-configuration workflow that creates one
> reviewed IntentPatch, applies it to one Task Requirements Table (TRT),
> reconciles that TRT with file-backed line state, generates one ScenarioSpec,
> executes one Isaac Sim scenario, extracts deterministic evidence and KPIs,
> and asks the operator whether to stop, revise, rerun, or perform a simulated
> deployment.

It is **not currently a multi-candidate strategy generator, strategy optimizer,
production scheduler, or physical deployment system**.

| Question | Audited answer |
| --- | --- |
| One or multiple candidate strategies? | One candidate patch and one reconciliation plan are produced for a release. The plan contains one decision per line, not alternative candidate strategies. One ScenarioSpec is produced and normally labeled `candidate_strategy_id: "primary"`. |
| Are multiple strategies ranked? | No. No strategy portfolio, objective function, Pareto method, score, or ranking mechanism is present in the release-to-simulation path. |
| Is a failed strategy regenerated automatically? | No. A simulation or evidence failure does not trigger automatic candidate regeneration. The operator can request revision or a rerun. Intent JSON may be retried after truncation/malformed output, but that is an extraction retry, not strategy optimization. |
| What does Isaac Sim do? | It executes one ScenarioSpec, records run behavior and KPI evidence, and supports feasibility/evidence checks for that one scenario. It does not compare alternative strategies. |
| Does the system schedule or optimize production? | No optimization or scheduling solver was found. It configures goals, line priority, tooling scope, pickup-order policies, abnormal behavior, KPIs, and simulation parameters. |
| What are the primary capabilities? | Natural-language classification/extraction, missing-field handling, structured IntentPatch/TRT/ScenarioSpec generation, deterministic validation, single-scenario simulation, evidence extraction, rejection explanations, and human review. |
| Physical deployment, Real-to-Sim, or Sim-to-Real? | No verified physical-equipment integration. The deployment endpoint updates local JSON files and is explicitly a simulated physical deployment. File/API state import exists, but no implemented sensor/PLC/robot telemetry pipeline or Sim-to-Real actuation pipeline was found. |

## 3. Candidate strategy capability

### 3.1 What is actually generated

There are three different objects that should not be conflated:

1. **IntentPatch**: one candidate change to the current TRT.
2. **Reconciliation plan**: one deterministic plan containing one decision for
   each enabled line.
3. **ScenarioSpec**: one executable simulation specification.

`trt_core.supervisor.reconcile_current_trt()` invokes
`build_reconciliation_plan()` once. For each line, `decide_line()` returns one
of:

- `NO_CHANGE`
- `IMMEDIATE_SWITCH`
- `WAIT_FOR_CHECKPOINT`
- `DEGRADED_SWITCH`
- `REJECT_INCOMPATIBLE`

`DEGRADED_SWITCH` may contain the fixed action
`APPLY_PRIORITY_ONLY_DELAY_INSTRUMENT_RESTRICTIONS`. This is a deterministic
fallback action for one line; it is not one member of a generated and ranked
strategy set.

The reconciliation plan contains:

- `plan_id`
- TRT identity/version
- `line_decisions`
- `overall_status`
- source hashes
- optional release and affected-line metadata

It does not contain `candidate_strategies`, an objective value, a ranking, or a
selected alternative.

### 3.2 Meaning of `candidate_strategy_id`

The ScenarioSpec schema requires a singular string named
`candidate_strategy_id`. The active n8n ScenarioSpec request sets it to
`"primary"`. This field is trace metadata; it does not prove that multiple
strategies were generated.

A scan performed during this audit found:

- 210 stored reconciliation plans.
- 0 plans with a `candidate_strategies` field.
- 0 plans with an `objective` field.
- 0 plans with a `ranking` field.
- 179 stored ScenarioSpecs.
- 175 ScenarioSpecs labeled `primary`, 2 labeled `cand_001`, and 2 with an
  empty/missing value in older artifacts.

The alternative labels do not correspond to an implemented comparison or
ranking procedure.

### 3.3 Selection and ranking

No implementation of the following was found in the runtime path:

- Candidate strategy enumeration.
- Objective-function evaluation across candidate strategies.
- Weighted KPI scoring.
- Pareto ranking.
- Search, mathematical programming, reinforcement learning, AutoML, or a
  scheduling optimizer.
- Selection of the best candidate from several Isaac runs.

Isaac KPI thresholds influence whether the **single simulated scenario** is
recommended or blocked. They do not select a winner among alternatives.

### 3.4 Failure and regeneration behavior

The current behavior is:

- **Malformed or truncated LLM extraction**: n8n retries the same intent
  extraction prompt once, increasing `max_tokens` from 20,000 to 200,000.
  Repeated failure returns `INTENT_LLM_TRUNCATED_AFTER_RETRIES`.
- **Rejected patch or reconciliation**: processing stops or asks the operator
  for revision.
- **Simulation/evidence failure**: the evidence response offers
  `REQUEST_REVISION`, `RERUN_SIMULATION`, and sometimes `CANCEL`.
- **Operator revision**: the operator supplies a new or revised request, which
  can produce a new candidate patch.

There is no automatic generation of a new strategy after simulation failure.
The checked-in chat workflow also routes `RERUN_SIMULATION` to a response saying
that the same ScenarioSpec context is preserved; it does not route that branch
back into the ScenarioSpec or Isaac run node. Therefore, the current workflow
does not implement an autonomous failure-recovery loop.

## 4. Digital twin role

The digital twin is a scenario-based Isaac Sim execution environment for UR5
tool sorting. Its implemented boundary includes:

- A released TRT and file-backed line state.
- A reconciliation plan.
- A JSON Schema-validated ScenarioSpec.
- Line/workspace/robot bindings.
- Isaac command compilation.
- A Windows-hosted Isaac runner accessed over HTTP.
- SQLite run output and RunArtifact reading.
- Deterministic evidence and KPI extraction.

| Function | Implemented? | Qualification |
| --- | --- | --- |
| Check one strategy's simulated feasibility | Yes, within coded scenarios and assertions | This is observed simulation/evidence feasibility, not a proof of real-world feasibility or exhaustive safety. |
| Calculate strategy KPIs | Yes | Isaac/RunArtifact rows and the evidence extractor report throughput, downtime, completion timing, reset cycles, priority behavior, placement, and batch gating where data exists. |
| Compare multiple strategies | No | No multi-strategy batch, common objective, or winner-selection stage exists. |
| Perform all three simultaneously | No | It performs the first two for one scenario, not the third. |

The evidence extractor can:

- Recalculate throughput from event timing.
- Compare actual throughput and downtime with TRT/ScenarioSpec targets.
- Detect missing or unreliable run data.
- Check pickup-priority deviations.
- Check table-batch gating.
- Report placement warnings.
- Summarize failed lines and failed checks.
- Determine `deployment_allowed`, `deployment_recommended`, and whether
  operator acknowledgement is required.

Placement warnings are not always treated as absolute blocking failures; the
risk tier and evidence rules determine whether acknowledgement is sufficient.
That distinction must be reported explicitly in research claims.

## 5. Scheduling and optimization

The system does not currently perform production scheduling or optimization in
the usual operations-research sense.

It can configure:

- Production goal.
- Line priority from 1 to 5.
- Tooling inclusion/exclusion and required scope.
- Manipulator pickup-order policy.
- KPI thresholds.
- Abnormal-event policy.
- Simulation settings.

The manipulator policies are predefined execution rules:

- `FCFS`
- `REQUIRED_FIRST`
- `UNWANTED_FIRST`
- `EXPLICIT_TOOL_ORDER`
- `EXPLICIT_TYPE_ORDER`

They influence the simulated pick sequence. The system does not derive an
optimal sequence from an objective function. Likewise, changing
`/lines/{line_id}/priority` configures a priority value; it does not invoke a
line scheduler.

The package dependencies contain no production optimization library such as
OR-Tools, Pyomo, PuLP, Optuna, or a scientific optimizer.

## 6. Human review and deployment boundary

Human review is implemented at two important points:

1. The operator reviews and approves, rejects, or requests revision of the
   candidate IntentPatch.
2. After simulation evidence, the operator can deploy, decline, request
   revision, or request a rerun according to the evidence status.

Release decisions are persisted in release records and audit bundles.

The endpoint named `/deployment/simulated-deploy` does not control physical
equipment. `trt_core/evidence_extractor/simulated_deployment.py`:

- Rechecks evidence.
- Updates local state-record JSON.
- Updates `data/digital_twin/default_simulation_config.json`.
- Writes a deployment audit JSON.
- Labels the operation `SIMULATED_PHYSICAL_DEPLOYMENT`.

Although the line registry labels `line_1` and `line_2` as
`PHYSICAL_OR_DIGITAL_TWIN` and `physical_available: true`, no PLC, OPC UA,
Modbus, ROS 2 hardware bridge, robot-controller command, or other physical
actuation client was found. This registry metadata is not evidence of a
physical deployment implementation.

Similarly:

- `/state/update` permits an external caller to submit state records.
- State records are validated and persisted to files.
- No implemented physical sensor/telemetry acquisition pipeline was found.

Accordingly, the repository has an integration hook for state import, but not
a verified Real-to-Sim system. It has a simulated deployment state update, but
not a Sim-to-Real system.

## 7. n8n and additional analysis modules

### 7.1 n8n implementation status

n8n orchestration is implemented. The stored access report from 2026-07-06
records:

- n8n API accessible.
- chat endpoint accessible.
- workflow found and active.
- Chat Trigger present.
- response mode using response nodes.
- candidate review and deployment-decision chat nodes.
- session save/load paths.
- config-query, help, cancel, evidence, and deployment routes.

The prompt inventory was verified again against live n8n on 2026-07-21. No new
chat execution was triggered for this read-only audit.

The modular runtime workflows are:

- `ChatOperatorTaskAllocationDemo`
- `IntentToPatchReviewDemo`
- `PatchReleaseApprovalDemo`
- `ReleasedTRTToReconciliationDemo`
- `GenerateScenarioSpecDemo`
- `RunArtifactToEvidenceSummaryDemo`
- `DeploymentApprovalDemo`

`IntegratedOperatorTaskAllocationDemo` is a separate integrated workflow
artifact and was previously reported inactive. The active chat design uses
sub-workflows.

The checked-in import JSON files may contain `"active": false`; this is not the
same as the active/published state reported by the live n8n API.

### 7.2 Implemented modules

| Module | Status | Actual role |
| --- | --- | --- |
| Chat/dialogue router | Implemented and used | Classifies task, query, help, cancel, clarification, approval, and deployment turns. |
| Intent extraction | Implemented and used | LLM extraction followed by deterministic normalization. |
| Required-field/session handling | Implemented and used | Preserves pending intent and obtains `operator_id` and `reason`. |
| Patch firewall/application | Implemented and used | Validates schema, version, operation, path, read-only fields, and resulting TRT semantics. |
| Supervisor reconciliation | Implemented and used | Produces deterministic per-line transition decisions. |
| Feasibility/constraint analysis | Distributed, not a separate optimizer | Patch validators, reconciliation, ScenarioSpec validation, Isaac runtime, and evidence extraction each enforce part of the constraints. |
| ScenarioSpec generator | Implemented and used | Creates one validated executable specification. |
| Isaac adapter/host client | Implemented and used when host runner is available | Compiles and submits one scenario and reads status/results. |
| Evidence extractor | Implemented and used | Produces KPI comparison, failures, warnings, and deployment recommendation. |
| Configuration-query module | Implemented and used | Reads current TRT/state/scenario/run/config records without creating a patch. |
| M12 metrics/report modules | Implemented as offline test/report tools | Collect metrics, manage provenance, seed tests, compare results, and generate reports/figures. They are not production strategy analyzers. |
| Simulated deployment | Implemented | Updates local state/default files after evidence and operator approval. |
| Multi-candidate ranker/optimizer | Not implemented | No candidate portfolio or best-strategy selection. |
| Production scheduler | Not implemented | No schedule construction or optimization solver. |
| Physical deployment adapter | Not implemented | No verified hardware actuation path. |

### 7.3 Implemented but not wired into the active path

`trt_core/intent_precheck.py` contains useful deterministic checks for:

- Missing/invalid lines.
- Missing/conflicting goals.
- Unsupported instruments.
- Read-only state requests.
- Restricted simulation settings.
- Ambiguous priority language.

Repository search shows it is imported by tests and an evaluation script, but
not by `trt_core.api` or the active n8n runtime flow. Therefore, its presence
must not be cited as an active runtime guardrail.

The n8n intent-extraction prompt tells the LLM to reject restricted settings
such as `layout_source`, `max_seed_trials`, `seed_db_path`, and
`reuse_precomputed_layouts`. That is an LLM instruction, not an independent
operator-text interceptor. If the model omits or misclassifies the restricted
term, downstream candidate validation may not know that the original request
contained it. This is a material limitation for error interception.

## 8. Intent Generator contract

### 8.1 Universal task-change fields

Every candidate IntentPatch must ultimately contain:

| Field | Type | Requirement |
| --- | --- | --- |
| `patch_id` | non-empty string | Generated by the workflow/backend. |
| `trt_id` | non-empty string | Must match the current TRT. |
| `base_version` | string matching `v[0-9]+` | Must match the current TRT version. |
| `operator_id` | non-empty string | Required for a task-change review. |
| `intent_text` | string | Required for a task-change review. |
| `reason` | string | Required for a task-change review. |
| `operations` | array of JSON Patch-like operations | May be empty only for a simulation-config-only request. |
| `status` | enum | Normally `REVIEWED` before release. |

Configuration inquiries, `help`, `cancel`, small talk, and review/deployment
decisions do not require a new IntentPatch.

### 8.2 Supported operator intent types

`KPI_UPDATE` is normalized to `KPI_LIMIT_UPDATE`.
`MULTI_LINE_POLICY_UPDATE` is a composite wrapper. `DRY_RUN_ONLY` is a control
modifier rather than an independent TRT field update.

| Intent type | Required intent-specific fields | Optional fields | Allowed values/range | Missing/invalid handling |
| --- | --- | --- | --- | --- |
| `TASK_GOAL_UPDATE` | Target scope/line and `goal` | Priority, KPI values | Goal: `ROUTINE_CLASSIFICATION`, `TRAUMA_SET_PRIORITY`, `BACKLOG_CLEARING` | Clarify missing goal/scope. Reject unknown goal through structured schema/normalization. |
| `INSTRUMENT_SCOPE_UPDATE` | Target scope/line and at least one included/excluded tool/type field | Tooling policy, target set | Tool IDs `tool_01`-`tool_27`; current normalized tool vocabulary; legacy instruments listed below | Unknown tool/type should clarify or reject. Resulting selected/excluded overlap is rejected. |
| `KPI_LIMIT_UPDATE` | Target scope/line and at least one concrete `kpi_updates` value | Other KPI values | `deadline_minutes`: integer or null, >=0; `max_downtime_seconds`: integer or null, >=0; `min_throughput_per_hour`: integer, >=0 | Missing concrete KPI value rejects normalization. Negative/type-invalid values fail schema/semantics. No upper throughput bound exists. |
| `PRIORITY_UPDATE` | Target scope/line and `priority` | None | Integer 1-5 | Missing value clarifies; out-of-range fails schema/semantics. |
| `MANIPULATOR_PRIORITY_UPDATE` | Target scope/line and `manipulator_priority` | Explicit tool/type order, reference types, tie breaker | Effective policies: `FCFS`, `REQUIRED_FIRST`, `UNWANTED_FIRST`, `EXPLICIT_TOOL_ORDER`, `EXPLICIT_TYPE_ORDER` | Missing scope clarifies. Explicit policies require their corresponding non-empty order. |
| `ABNORMAL_STRATEGY_UPDATE` | Target scope/line and `abnormal_strategy` | Simulation intervention mode | TRT values: `STOP_LINE`, `CONTINUE_FEASIBLE_TASKS`, `ASK_OPERATOR` | Unknown values fail. `ASK_OPERATOR` must be resolved to `STOP_LINE` or `CONTINUE_FEASIBLE_TASKS` before ScenarioSpec execution. |
| `TOOLING_POLICY_UPDATE` | Target scope/line and target set, tooling scope, or concrete tool/type selection | Included/excluded/required tools | Target set currently `ENT_SURGICAL_TOOLING_SET`; required scope values listed below | Missing target clarifies. Unknown set/type rejects or requires clarification. |
| `SIMULATION_CONFIG_UPDATE` | Non-empty `simulation_config_updates` | Target scope may be inferred for some simulation-only paths | Field contract listed below | Empty update rejects. Type/range/enum violations fail structured output or normalization. |
| `DRY_RUN_ONLY` | `dry_run_only: true` combined with a supported request | `failure_action_hint` | Boolean | It suppresses deployment; it is not a replacement for the requested change fields. |
| `MULTI_LINE_POLICY_UPDATE` | Non-empty `sub_requests`; each sub-request has target and relevant fields | Shared simulation update | Current registered lines only | Empty sub-request list or a sub-request without a concrete update rejects. |

The following strings also appear in `REQUEST_TYPES`, but they are diagnostic
or evaluation labels rather than user-facing change intents:

- `single_line_patch`
- `multi_line_request`
- `missing_line`
- `missing_goal`
- `invalid_line`
- `unsupported_instrument`
- `read_only_state_request`
- `conflicting_goal`

### 8.3 Current permitted domain vocabulary

The current deployed TRT is `trt-demo@v171` and contains four registered lines.

| Category | Current values |
| --- | --- |
| Line IDs | `line_1`, `line_2`, `line_3`, `line_4` |
| Workspaces | `workspace_line_1` through `workspace_line_4` |
| Robots | `ur5_line_1` through `ur5_line_4` |
| Line type | `UR5_SORTING_CELL` |
| Low-level digital-twin task name | `ur5_pick_place` |
| Production goals | `ROUTINE_CLASSIFICATION`, `TRAUMA_SET_PRIORITY`, `BACKLOG_CLEARING` |
| Target set | `ENT_SURGICAL_TOOLING_SET` |
| Tool IDs | `tool_01` through `tool_27` |
| Normalized tool types | `FORCEPS`, `SCISSORS`, `DOUBLE_ENDED_SURGICAL_RETRACTOR`, `SURGICAL_FORCEPS`, `KNIFE_HANDLE`, `SPONGE_FORCEPS`, `NEEDLE_HOLDER`, `NERVE_RETRACTOR`, `MASTOID_RETRACTOR`, `SURGICAL_SUCTION_CANNULA` |
| Legacy instrument enum | `SCISSORS`, `FORCEPS`, `CLAMPS`, `RETRACTOR` |
| Tooling required scope | `ALLOWED_INSTRUMENTS`, `ALL_SUPPORTED_INSTRUMENTS`, `ALL_SUPPORTED_TOOLING`, `SELECTED_TOOLING`, `NONE` |
| State mode | `IDLE`, `RUNNING`, `INTERVENTION`, `PAUSED`, `ERROR` |
| Checkpoint | `NONE`, `TRAY_COMPLETE`, `BATCH_COMPLETE`, `MANUAL_CLEARANCE_REQUIRED` |

The operator cannot create a new task name, robot, workspace, or workstation
through an IntentPatch. Those fields are not on the writable-path whitelist.
The current extraction schemas enumerate only `line_1` through `line_4`; there
is no implemented natural-language mechanism to generate a 99-line TRT.

### 8.4 Simulation configuration

| Field | Type/range |
| --- | --- |
| `headless` | boolean or null |
| `dry_run_only` | boolean or null |
| `num_envs` | integer or null, minimum 1 |
| `global_seed` | integer or null, minimum 0 |
| `reuse_verified_seed` | boolean or null |
| `add_reference_number` | integer or null, minimum 0 |
| `allowed_overlap_ratio` | number or null, minimum 0 |
| `chosen_intervention_mode` | `continue-until-arrival`, `immediate-stop`, or null |
| `travel_time` | number or null, minimum 0 |
| `fix_duration` | number or null, minimum 0 |
| `resume_delay` | number or null, minimum 0 |
| `episode_success_requires_reset_cycles` | integer or null, minimum 1 |

Important gaps:

- The schema does not set a maximum for `num_envs`,
  `add_reference_number`, or `allowed_overlap_ratio`.
- The active four-line registry effectively limits executable line bindings to
  four, but this is not expressed as a `maximum: 4` in the intent schema.
- `allowed_overlap_ratio` is called a ratio but has no schema maximum of 1.
- KPI values have no domain-specific upper plausibility bound.
- Consequently, a throughput target such as `999999` is schema-valid. It may
  be physically unrealistic, but the current IntentPatch validator should not
  claim it is invalid unless an explicit plausibility rule is added.

### 8.5 Action names

The system has several action vocabularies:

- Chat turn types: `SMALL_TALK`, `TASK_REQUEST`,
  `CLARIFICATION_VALUES`, `APPROVAL_DECISION`, `DEPLOYMENT_DECISION`,
  `HELP`, `CONFIG_QUERY`, `CANCEL`, `CONFUSED`, `UNKNOWN`.
- Patch review decisions: `APPROVE`, `REJECT`, `REQUEST_REVISION`.
- Post-evidence decisions: `DEPLOY`, `DEPLOY_WITH_ACK`, `DO_NOT_DEPLOY`,
  `RERUN_SIMULATION`, `REQUEST_REVISION`.
- Runtime JSON Patch operations: `test`, `add`, `replace`, `remove`.

The IntentPatch JSON Schema also lists `move` and `copy`, but the runtime
firewall rejects them. The runtime set is the effective contract.

### 8.6 Ambiguity and missing fields

The intended behavior is:

- Missing `operator_id` or `reason`: preserve the pending intent and ask only
  for those fields.
- Missing target line/scope: ask which line or whether all lines are intended.
- Ambiguous use of “priority”: ask whether the operator means production-line
  priority or robot pickup order.
- Conflicting goals: ask the operator to choose one.
- Unknown tool/type/target set: clarify or reject.
- `help` and `cancel`: route before required-field and patch logic.

The active runtime relies significantly on LLM classification for ambiguity.
The deterministic precheck that would independently detect several of these
conditions is not wired into the active route. Therefore, the behavior is
implemented but not uniformly guaranteed end to end.

### 8.7 Inquiry versus task-change routing

The dialogue model classifies inquiry requests as `CONFIG_QUERY`. They are
handled by `/chat/config-query` and do not create a patch.

Supported query targets are:

- `TIME_ARRIVAL_MODEL`
- `STATE_RECORDS`
- `LINE_STATE`
- `KPI_TARGETS`
- `TASK_REQUIREMENT_TABLE`
- `TRT_CURRENT`
- `TRT_HISTORY`
- `DEPLOYMENT_HISTORY`
- `SCENARIO_SPEC`
- `RUN_ARTIFACT`
- `ISAAC_COMMAND_CONFIG`

A task-change request follows the candidate patch, validation, review, release,
reconciliation, ScenarioSpec, simulation, and evidence path.

### 8.8 Invalid workflow interception

Interception is distributed across layers:

1. n8n required-field and session-state routing.
2. LLM structured-output schema.
3. Domain candidate normalization.
4. IntentPatch JSON Schema.
5. Base-version and TRT identity checks.
6. Runtime operation/path whitelist and read-only state guard.
7. Resulting TRT schema and semantic checks.
8. Supervisor state/registry reconciliation.
9. ScenarioSpec schema, source alignment, line binding, and executable-strategy checks.
10. Isaac runner/runtime validation.
11. RunArtifact/evidence validation.
12. Evidence-aware simulated deployment guard and human decision.

Examples of deterministic semantic rejection include:

- Allowed and excluded instruments overlap.
- Selected and excluded tool IDs overlap.
- Priority is outside 1-5.
- Explicit order policy has no order list.
- KPI value is negative or has the wrong type.
- `CONTINUE_FEASIBLE_TASKS` is requested while a line state is `ERROR`.
- A patch targets a stale TRT version.
- A patch writes a non-whitelisted or read-only path.

## 9. Prompt and structured-output design

### 9.1 End-to-end conversion

```text
Operator natural language
  -> n8n Chat Trigger and session load
  -> dialogue-decision structured JSON
  -> clarification, query, control action, or task path
  -> domain intent structured JSON
  -> deterministic Python normalization
  -> IntentPatch JSON operations
  -> schema/firewall/semantic validation
  -> human release decision
  -> released TRT
  -> deterministic reconciliation plan
  -> ScenarioSpec JSON
  -> Isaac host-runner HTTP request
  -> SQLite RunArtifact
  -> deterministic evidence/KPI summary
  -> human post-evidence decision
  -> optional simulated local-state deployment
```

The LLM does not directly write the released TRT, ScenarioSpec, Isaac SQLite
database, evidence decision, or physical equipment command.

### 9.2 Prompt objectives and ownership

The runtime contains six chat-completions prompt roles:

| Component | Objective | Owner/location | Explicit reasoning feature |
| --- | --- | --- | --- |
| Dialogue decision | Classify the turn and extract a compact normalized request | `trt_core.api` | No |
| Configuration answer formatter | Format deterministic source-backed query results | `trt_core.api` | No |
| Priority clarification fallback | Resolve one closed-choice ambiguity | `trt_core.chat_sessions` | No |
| Intent domain extractor | Extract domain fields without creating JSON Patch | n8n `IntentToPatchReviewDemo` | No |
| Intent extractor retry | Retry malformed/truncated extraction | n8n `IntentToPatchReviewDemo` | No |
| Operator response formatter | Format canonical workflow output for chat | n8n active chat workflow | No |

The prompts are manually authored static strings in Python and n8n workflow
nodes. The repository does not identify an individual prompt author. No LLM
creates or self-revises the prompts at runtime.

The client request bodies do not set `reasoning`, `reasoning_effort`,
`enable_thinking`, or equivalent options. Captured model responses contained
`reasoning: null`. This means separate reasoning output is not explicitly
requested; it does not establish what the remote model server does internally.

The exact current prompt inventory is documented in
`docs/trt_api_n8n_chat_completions_prompt_inventory.md`.

### 9.3 LLM assistance versus deterministic work

LLM-assisted:

- Chat-turn classification.
- Natural-language field extraction.
- One fallback ambiguity classification.
- Operator-facing formatting.
- One retry when extraction JSON is incomplete.

Manually organized and deterministically verified:

- Supported fields, enums, and schemas.
- Current TRT, line, tool, and set context.
- JSON Patch construction.
- Writable/read-only paths.
- Patch application and versioning.
- Reconciliation decisions.
- ScenarioSpec construction.
- Isaac command compilation.
- Evidence extraction and deployment recommendation.
- Release and deployment decisions.

An operator revision causes the revised natural-language request to be parsed
again. The model does not autonomously diagnose a failed simulation and invent
a replacement strategy.

### 9.4 Prompt restrictions

The prompts and structured schemas restrict:

- **Field names** through `additionalProperties: false`.
- **Required fields** through JSON Schema `required` arrays.
- **Data types** through string/integer/number/boolean/object/array schemas.
- **Allowed values** through dynamic and static enums.
- **Numeric ranges** through minimum/maximum constraints where present.
- **Output format** through vLLM `structured_outputs: {json: ...}` and
  “return only JSON” instructions.
- **Prohibited content** by instructing the extractor not to create patch IDs,
  TRT IDs, status, JSON Patch operations, infrastructure simulation settings,
  or unsupported domain values.
- **Data provenance in query answers** by instructing the formatter to use
  only supplied structured data and not invent missing values.

These restrictions are layered. A prompt instruction alone is not treated as
a safety guarantee; Python and JSON Schema checks are the stronger boundary.

### 9.5 JSON Schema, examples, and post-processing

The implementation uses all of the following:

- JSON Schema Draft 2020-12 for TRT, IntentPatch, ScenarioSpec, release, and
  audit records.
- vLLM structured-output JSON schemas for dialogue and intent extraction.
- Dynamic enums from the current TRT through `/intent/context`.
- Few-shot examples for a valid trauma-priority patch, a valid instrument
  exclusion, and an invalid semantic conflict.
- Numerous inline examples in the dialogue and extraction system prompts.
- Deterministic JSON parsing and normalization.
- Patch firewall and semantic validation after extraction.
- ScenarioSpec validation before export/run.
- Deterministic source-detail checks for formatted configuration answers.
- Deterministic fallback operator text when formatter output is invalid.

### 9.6 Error handling

| Error | Current behavior |
| --- | --- |
| LLM returns `finish_reason: length` | Retry intent extraction with a larger token limit. |
| LLM returns malformed/empty JSON | Retry once; then return a structured system error. |
| Dialogue model call fails | Return `UNKNOWN` with an operator-visible evaluation error. |
| Required operator fields missing | Preserve session and request only missing values. |
| Domain field missing/ambiguous | Clarify or reject during normalization. |
| Patch schema/path/version invalid | Reject before TRT application and record reasons. |
| Resulting TRT semantically inconsistent | Reject application and record semantic reasons. |
| Reconciliation not ready/rejected | Stop ScenarioSpec execution or wait for checkpoint. |
| ScenarioSpec malformed/inconsistent | Raise a ScenarioSpec generation error. |
| Isaac/RunArtifact unavailable | Return simulation/evidence failure; do not fabricate evidence. |
| Evidence blocks deployment | Do not recommend/allow simulated deployment; show failure reasons and revision/rerun options. |

### 9.7 Prompt and schema gaps

The following gaps should be corrected before claiming a fully constrained
intent interface:

1. The dialogue and n8n intent prompts state Time-Arrival defaults of
   `travel_time=5.0`, `fix_duration=8.0`, and `resume_delay=0.5`.
   `data/digital_twin/default_simulation_config.json` currently stores
   `1.0`, `3.0`, and `1.0`. Relative instructions may therefore be compiled
   against stale defaults.
2. Some schemas list `HIGHEST_RISK_FIRST` and `LOWEST_RISK_FIRST`, while the
   implemented extractor accepts only the five policies listed earlier.
3. The IntentPatch schema lists `move` and `copy`, while the runtime firewall
   rejects them.
4. Numeric upper plausibility limits are missing.
5. Line IDs are effectively fixed to four in extraction schemas even though
   parts of the TRT schema are structurally more general.
6. The deterministic intent precheck is not wired into the active path.
7. There are two related extraction paths: the `trt-api` dialogue route can
   construct a candidate directly, while the n8n intent-review sub-workflow
   has its own domain extractor. This duplicates mapping logic and increases
   the risk of inconsistent interpretation.
8. The response-formatting LLM is constrained and has fallbacks, but it remains
   another possible communication-loss stage if it paraphrases or omits
   evidence detail without triggering a deterministic completeness check.

## 10. Test and result evidence

### 10.1 Reviewed trial

The 27-item reviewed M12 trial reported:

- 17 PASS.
- 10 FAIL.
- TC1: 4 PASS, 4 FAIL.
- TC2: 6 PASS, 3 FAIL.
- TC3: 2 PASS, 0 FAIL.
- TC4: 5 PASS, 3 FAIL.

Its human review explicitly corrected several automated scores. For example,
source-backed KPI/state/task-table answers were sometimes correct even when the
automated scorer marked them failed. Conversely, several unsafe/unsupported
requests reached candidate approval despite optimistic automated scores.

This demonstrates human-review support and real execution traces, but it also
shows that the automated scorer is not a reliable authority on semantic
correctness.

### 10.2 Full comparison run

The manual packet defines 174 rows:

- TC1: 44.
- TC2: 75.
- TC3: 30.
- TC4: 25.

The archived `rescored_full_run_v3` summary contains 173 rows and many
`INCONCLUSIVE`, `DATA_MISSING`, `TRACE_MISSING`, and `VALIDATION_FAILED`
statuses.

The repository's own automated-run validity audit concluded:

- TC4 required retesting because many stage-specific errors were never
  injected.
- TC2 pass-rate claims were invalid because required tools/order/arguments
  were not actually verified.
- TC1 and TC3 raw timing/RunArtifact evidence could be useful, but correctness
  required rescoring.

Therefore, the full comparison outputs should not be used to claim candidate
optimization, complete error interception, or end-to-end correctness without
case-level adjudication.

### 10.3 Static test-suite caveat

Some n8n unit-test assertions still reference older node names such as
`Build vLLM Chat Turn Parse Body`, while the checked-in active design uses
`Build vLLM Dialogue Decision Body`. This indicates that not every workflow
test should be assumed current merely because the test file exists.

### 10.4 What the artifacts do prove

The stored artifacts do support these narrower conclusions:

- Natural-language requests reached n8n and `trt-api`.
- Candidate patches, releases, reconciliation plans, ScenarioSpecs, run IDs,
  and RunArtifacts were produced in successful cases.
- One recorded scenario is associated with one reconciliation plan and one
  run.
- Evidence summaries can contain KPI, priority, placement, batch, and
  deployment-decision information.
- No stored reconciliation-plan portfolio or strategy ranking was observed.

## 11. Recommended research wording

Claims supported by the implementation:

- “The system translates operator natural language into a validated,
  versioned Task Requirements Table update.”
- “The system generates an executable ScenarioSpec and evaluates one proposed
  policy in Isaac Sim.”
- “The system extracts evidence and KPI results from the RunArtifact and
  presents them for human review.”
- “The system can block or discourage simulated deployment when validation or
  evidence fails.”
- “Human review is retained before release and after digital-twin evidence.”

Claims not currently supported:

- “The system generates multiple candidate strategies.”
- “The system selects the optimal strategy.”
- “The system optimizes production scheduling.”
- “The system automatically repairs a failed strategy.”
- “The system deploys to physical production equipment.”
- “The system implements Real-to-Sim or Sim-to-Real synchronization.”

A precise contribution statement would be:

> The contribution is an evidence-gated, human-in-the-loop translation and
> verification pipeline for one operator-proposed production-line policy at a
> time. It combines structured intent extraction, versioned task-requirement
> updates, state-aware reconciliation, physics-based digital-twin execution,
> deterministic evidence extraction, and deployment suppression. It does not
> yet optimize among alternative strategies or close the loop with physical
> equipment.

## 12. Primary source map

- Candidate/reconciliation logic: `trt_core/supervisor.py`,
  `trt_core/reconciliation.py`
- Patch release and review records: `trt_core/release.py`,
  `trt_core/repository.py`
- Intent extraction/normalization: `trt_core/api.py`,
  `trt_core/intent_normalizer.py`, `trt_core/intent_precheck.py`
- Patch validation: `trt_core/validator.py`, `trt_core/patch_apply.py`,
  `trt_core/semantic_rules.py`
- Intent/TRT schemas: `schemas/intent_patch.schema.json`,
  `schemas/trt.schema.json`
- ScenarioSpec generation: `scenario_generation/generator.py`,
  `schemas/scenario_spec.schema.json`
- Isaac adapter: `trt_core/digital_twin_adapter/`
- Evidence and deployment: `trt_core/evidence_extractor/`
- n8n workflows: `n8n_workflows/`
- Current line registry: `data/production_lines/line_registry.json`
- Current TRT: `data/trt/current_trt.json`
- Current persisted simulation defaults:
  `data/digital_twin/default_simulation_config.json`
- Prompt inventory:
  `docs/trt_api_n8n_chat_completions_prompt_inventory.md`
- Reviewed M12 result:
  `outputs/reports/m12/automated_smoke_n8n_run_20260706_193732/human_reviewed/`
- Full-run validity audit:
  `outputs/reports/m12/_archive/pre_smoke_retest_20260706_193553/automated_run_validity_audit/`
