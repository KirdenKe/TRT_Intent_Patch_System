# Pre-Smoke System Readiness Audit

Generated: 2026-08-05

## 1. Candidate generation and selection semantics

### 1.1 Model identity and candidate count

One configured LLM generates the complete candidate batch in one structured-output
request. The production path does not mix Gemma, Qwen, and Llama outputs. The
model and endpoint are recorded in `generation_provenance`; the current defaults
are Gemma at the configured `VLLM_CHAT_COMPLETIONS_URL`. A batch contains 2-8
candidates. A deterministic-output retry uses the same model and endpoint.

TC6 and TC7 are separate experiments. TC6 repeats one model/prompt/input. TC7
compares Gemma, Qwen, and Llama. Their outputs are not combined into a production
candidate batch.

### 1.2 Regeneration boundary

The LLM may regenerate the complete batch only before simulation when its output
is malformed, schema-invalid, changes an operator-locked constraint, references
an unknown line, or is not behaviorally distinct. Validation feedback is supplied
to the same model for at most the configured bounded number of attempts.

The LLM is not called again because a candidate fails ScenarioSpec generation or
Isaac evidence validation. Each candidate that compiles receives one ScenarioSpec
and one sequential Isaac run. A ScenarioSpec failure produces no RunArtifact for
that candidate. A completed but ineligible RunArtifact is retained as evidence.

If no candidate is eligible, the system sets `operator_refinement_required=true`,
does not select or deploy anything, and returns evidence-derived suggestions. A
simulator/API failure should be corrected by engineering first; shared physical or
semantic failures should cause the operator to refine the intent.

### 1.3 Approval and selection ownership

The operator approves the IntentPatch once. That approval authorizes generation
and non-deployment simulation of every candidate inside the approved constraints.
It is not approval to deploy every candidate.

The system applies mandatory gates and selects the eligible candidate with the
highest measured throughput attainment. The operator sees the candidate names,
rationales, constraint results, KPIs, RunArtifact IDs, and selected candidate. The
operator can then deploy the system-selected candidate, decline it, or request
revision. The current system does not provide a post-simulation override for the
operator to select a lower-ranked candidate.

The phrase "selected ScenarioSpec" means the ScenarioSpec belonging to the
system-selected candidate may proceed to simulated physical deployment. All
successfully compiled candidate ScenarioSpecs have already been run in the
digital twin.

## 2. Selection objective

The objective is `constraint_gated_throughput_v3`.

Mandatory constraints are:

1. RunArtifact completed.
2. Evidence allows deployment.
3. Every recorded placement verification passed (`R_storage = 1`).
4. Priority deviation count is zero.
5. Batch-gating violation count is zero.
6. Throughput evidence is present.
7. The actual throughput of every individual simulated line is at least that line's target throughput.

`R_reset` is retained as a non-blocking diagnostic metric and is not an
optimization term. Missing reset evidence produces `DATA_INCOMPLETE` and a
diagnostic warning, but does not by itself disqualify a candidate.
Placement, priority, and batch-gating evidence are also hard constraints, not
tradeable score components.

Eligible candidates are ranked by:

```text
score = throughput_attainment
throughput_attainment = mean(actual throughput / target throughput across simulated lines)
```

The mean is used only to rank candidates that already pass the per-line gate. A
high-throughput line cannot compensate for another line that misses its target.

The ratio is not capped at 1.0. Candidate workload, target lines, task meaning,
tooling targets, KPI targets, and Time-Arrival values are locked so throughput is
compared over the same requested problem. Ties use lower in-simulation strategy
duration and then candidate ID.

## 3. Digital twin role

Isaac Sim evaluates one candidate at a time. It checks physical executability and
produces placement, ordering, reset, timing, and KPI evidence. It does not itself
compare candidates. Cross-candidate constraint gating and throughput ranking are
performed deterministically by `trt-api` after all candidate runs complete.

The system proposes and evaluates policy alternatives. It is not a job-shop
scheduler, production optimizer, or global task-allocation solver. It does not
claim mathematical optimality beyond the evaluated candidate batch.

## 4. Deployment and Time-Arrival state

An accepted simulated deployment updates:

- `data/state_records/current_state.json` and line state records.
- `data/digital_twin/default_simulation_config.json`.
- `data/state_records/time_arrival_model.json`.
- The deployment audit record.

It does not update `data/scenario_templates.json`. That file is a template/fallback
registry, not the authoritative deployed Time-Arrival state. Smoke tests always
reply `DO_NOT_DEPLOY`, so they must not modify deployed state.

## 5. Workstations

| Workstation | Function | Input | Task | Output | Major limitations |
| --- | --- | --- | --- | --- | --- |
| Operator chat | Intent and review interface | Natural language | Clarify, approve, reject, revise | Chat/review events | Human communication latency |
| Intent generator | Structured request generation | Chat plus current TRT | Classify, extract, validate | IntentPatch/clarification | Supported schema only |
| Supervisor/reconciliation | State alignment | Released TRT and state records | Reconcile affected lines | Reconciliation plan | Not a scheduler |
| Candidate generator | Alternative policies | Released TRT, state, Time-Arrival state | Generate 2-8 candidates | Candidate batch | One configured LLM per batch |
| Scenario adapter | Executable specification | Candidate and scene contract | Generate/validate ScenarioSpec | ScenarioSpec | Cannot fix infeasible intent |
| Isaac Sim | Physical what-if evaluation | ScenarioSpec | Simulate physical behavior | RunArtifact/SQLite | Fidelity and startup cost |
| Evidence/selector | Safety gate and ranking | RunArtifacts and constraints | Gate, compare, select | Winner or refinement request | Requires complete evidence |

## 6. Experiment cases

| Case | Initial conditions | Expected problem | Test objective | Methodology | Expected result |
| --- | --- | --- | --- | --- | --- |
| TC1 | Gold operator intents and current state | Parse/schema/semantic drift | Intent-to-plan correctness | Compare patch and ScenarioSpec with fixed expected fields | Correct plan or justified clarification/rejection |
| TC2 | L1/L2/L3 gold queries | Wrong tool, order, argument, or answer | Evidence-query orchestration | Compare trace and answer with fixed gold sequence | Sourced, dependency-correct result |
| TC3 | Defined physical what-if setups | Missing/failed KPI evidence | KPI and physical validation | Sequential candidate simulations and stored metrics | Constraint-gated throughput winner or refinement |
| TC4 | Defined injected invalid/unsafe state | Error escapes required gate | Error interception | Inject at the specified stage | Interception before deployment |
| TC5 | Full lifecycle request | Missing lifecycle/review timestamps | Closed-loop timing | Record intent through manual review | Equations 3.4-3.6 or DATA_INCOMPLETE |
| TC6 | Same model, prompt, and input | Unstable structured generation | Single-model stability | Repeated requests | Format/completeness/semantic/variation metrics |
| TC7 | Same fixtures across three models | Model-dependent quality/cost | Model comparison | Gemma/Qwen/Llama requests | Quality, latency, token, and provenance table |

## 7. Outcome, checkpoint, and review reporting

Future reports distinguish Autonomous Success, Manually Assisted Success,
Validation Failure, Input Failure, Simulation Failure, System Error, Manual
Rejection, and Evaluation Incomplete when evidence is missing. Assisted cases are
not included in autonomous success.

The report computes autonomous success rate, assisted completion rate, and overall
completion rate. CP0-CP6 are reported with checkpoint-specific denominators. The
appendix consistently uses the term `manual review` and preserves `reviewer_type`
so a Codex/engineer semantic review is not misrepresented as an operator decision.

Automated pass rate, manual-review pass rate, auto-manual agreement, both
disagreement directions, reasons, failure sources, correction methods, and a
defined overall compliance denominator are included. Summary SVGs cover
checkpoints, automated/manual review, and outcome classes. Per-case evidence is
provided as complete tables and transcripts rather than one chart per case.

The detailed appendix records every instruction, expected criterion, full
interaction, CP0-CP6 results, automated and manual verdicts, outcome, failure
source, correction, session/execution IDs, strategy batch, every candidate run ID,
the selected RunArtifact, metrics, formulas, and data-quality status.

## 8. Timing scope

`T_verification_wall = T_artifact_created - T_scenario_created` is retained as
the raw wall interval. The thesis verification metric is now:

```text
T_verification = T_verification_wall - T_isaac_startup
```

`T_isaac_startup` starts when the host runner launches the Isaac command and ends
at the last configured articulation or GPU startup warning observed by the host.
The host system timestamp is primary; an Isaac internal timestamp is an explicit
fallback. If neither can be measured, `T_verification` is null and the row is
`DATA_INCOMPLETE`.

`strategy_simulation_seconds` is derived from in-simulation sorting evidence and
does not include initial launch/render time. The worker records combined startup
and execution wall time separately. Startup alone remains null and explicitly
marked `SIMULATION_STARTUP_NOT_SEPARABLE_FROM_HOST_WALL_TIME` until the host runner
emits a dedicated startup-complete event.

## 9. Revised smoke queue

The core readiness queue contains the original 27 cases: 8 TC1, 9 TC2, 2 TC3,
and 8 TC4. This preserves the accepted literature-comparison denominator.
Simulation cases use 4 x 2, 2 x 4, or 2 x 5 line/tool configurations, so no case
exceeds four lines or ten total tools. Candidate simulations are sequential and
deployment is disabled.

The complete smoke suite has 30 checks. SMOKE_028 (TC5) checks the lifecycle of
a live core run. SMOKE_029 (TC6) repeats one Gemma request three times.
SMOKE_030 (TC7) sends the same fixture three times to Gemma, Qwen, and Llama.
These extension rows do not alter the 27-case TC1-TC4 literature denominator.

The queue removes duplicate queries, nonexistent seed run IDs, stale Time-Arrival
expectations, `line_99` as an inherently invalid request, and `999999` throughput
as an inherently invalid request. Deterministic invalid-range tests use a negative
line identifier and negative throughput.

## 10. Readiness evidence and residual risk

- Active n8n Chat Trigger: reachable through an isolated `help` probe.
- Active candidate workflow: imported, published, restarted, and exported for verification.
- `trt-api`: healthy and loading `constraint_gated_throughput_v3`.
- Focused revised tests: 20 passed.
- Broad legacy suite: 302 passed and 42 failed. The failure count matches the prior known baseline and includes stale path, workflow-shape, mutable-state, and old Time-Arrival-default assertions. It remains regression debt and must not be hidden in the final report.
- Authenticated n8n API access was reverified after the key was refreshed. The runner prefers the API and retains a verified fallback to the local n8n SQLite execution store with `LOCAL_N8N_SQLITE` provenance.
- No smoke simulation was launched during this audit.

The system is ready to begin the revised smoke queue as a validation run, not as
proof that the full system is complete. Any failed smoke checkpoint must be
manually reviewed and classified before a full test is authorized.
