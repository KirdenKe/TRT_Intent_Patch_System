# TRT Intent Patch System

Deterministic prototype core for Section 3.2: Task Requirements Table (TRT), Intent Patch, validation firewall, versioned repository, and Audit Bundle generation.

n8n is treated only as an orchestration layer. Validation, patch application, versioning, and audit generation live in the Python package.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Run Tests

```powershell
pytest
```

## Repository Storage

Accepted TRT versions are stored as JSON files in:

```text
data/trt_versions/
```

Accepted and rejected patch attempts both create immutable Audit Bundles in:

```text
data/audit_bundles/
```

The prototype uses `v1`, `v2`, `v3` style versions and SHA-256 hashes over canonical JSON snapshots.

Release records are stored as JSON files in:

```text
data/releases/
```

Release statuses are `PENDING_OPERATOR_DECISION`, `RELEASED`, `REJECTED_BY_OPERATOR`, `NEEDS_REVISION`, and `FAILED_STALE_VERSION`.

## API

Start the FastAPI app:

```powershell
uvicorn trt_core.api:app --reload
```

Endpoints:

- `POST /patch/validate` validates an Intent Patch against the current TRT without writing a new TRT or Audit Bundle.
- `POST /patch/apply` validates and applies an Intent Patch. It always writes an Audit Bundle. Accepted patches write exactly one new TRT version.
- `GET /intent/context` returns the current TRT plus enum/path context, `llm_candidate_generation_schema`, and `intent_patch_internal_schema`.
- `POST /intent/normalize` converts a domain-level candidate from vLLM into a full Intent Patch.
- `POST /release/prepare` stores a reviewed candidate patch as a pending release record without applying it.
- `POST /release/decision` records `APPROVE`, `REJECT`, or `REQUEST_REVISION`; approval delegates to existing patch application.
- `GET /release/{release_id}` returns the persisted release record.
- `GET /state/current` returns the current production-line State Records.
- `POST /state/update` stores the latest production-line State Records.
- `POST /supervisor/reconcile` creates a Supervisor Reconciliation Plan from the current released TRT and State Records.
- `GET /reconciliation/{plan_id}` returns a persisted Reconciliation Plan.
- `GET /trt/current` returns the latest TRT. Optional query parameter: `trt_id`.
- `GET /audit/{audit_id}` returns an Audit Bundle.

Example:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/patch/apply `
  -ContentType "application/json" `
  -InFile tests/fixtures/valid_patch.json
```

## vLLM n8n Review Workflow

The workflow at `n8n_workflows/intent_to_patch_review.workflow.json` accepts natural-language operator intent and uses vLLM only as a candidate generator. It does not release or apply patches.

Flow:

- n8n receives operator intent at `POST /webhook/intent-to-patch-review`.
- n8n fetches Python context from `GET /intent/context`.
- n8n calls vLLM at `http://192.168.50.168:27783/v1/chat/completions` with top-level `structured_outputs.json` set to `llm_candidate_generation_schema`.
- vLLM extracts `action`, `line_id`, `goal`, `excluded_instruments`, clarification questions, unsupported terms, and detected request types, not raw JSON Patch operations or metadata.
- n8n rejects vLLM responses with `finish_reason: "length"` because the JSON may be incomplete.
- If `action` is `NEEDS_CLARIFICATION` or `UNSUPPORTED_REQUEST`, n8n returns a revision response without calling Python normalization or validation.
- If `action` is `PROPOSE_PATCH`, n8n attaches `patch_id`, `trt_id`, `base_version`, `operator_id`, `intent_text`, `reason`, and `status: REVIEWED`.
- Python `POST /intent/normalize` converts the candidate to an Intent Patch.
- Python `POST /patch/validate` performs deterministic validation.
- n8n returns `REVIEWED` or `NEEDS_REVISION`.

Import the workflow in n8n:

```sh
n8n import:workflow --input=/home/node/.n8n/imports/intent_to_patch_review.workflow.json
```

Start the Python API before running the workflow:

```powershell
uvicorn trt_core.api:app --host 0.0.0.0 --port 8000
```

Test the workflow with curl:

```sh
curl -X POST http://localhost:5678/webhook/intent-to-patch-review \
  -H "Content-Type: application/json" \
  -d '{
    "operator_id": "op_001",
    "intent_text": "Make Line 1 prioritize Trauma Set and exclude forceps",
    "reason": "urgent trauma set deadline"
  }'
```

Response meanings:

- `REVIEWED`: Python accepted the normalized candidate as valid. Operator confirmation is still required before any release or `/patch/apply` call.
- `NEEDS_REVISION`: Python rejected the candidate. Use `rejection_reasons` to revise the operator request or prompt.

Sample successful response:

```json
{
  "status": "REVIEWED",
  "candidate_patch": {
    "patch_id": "patch-1790000000000",
    "trt_id": "trt-demo",
    "base_version": "v1",
    "operator_id": "op_001",
    "intent_text": "Make Line 1 prioritize Trauma Set and exclude forceps",
    "reason": "urgent trauma set deadline",
    "operations": [
      {
        "op": "replace",
        "path": "/lines/line_1/goal",
        "value": "TRAUMA_SET_PRIORITY"
      },
      {
        "op": "replace",
        "path": "/lines/line_1/excluded_instruments",
        "value": ["FORCEPS"]
      }
    ],
    "status": "REVIEWED"
  },
  "validation_results": {
    "schema": true,
    "path_whitelist": true,
    "readonly": true,
    "base_version": true,
    "semantic": true
  },
  "message": "Candidate patch is valid. Operator confirmation is required before release."
}
```

Schema boundary:

- `llm_candidate_generation_schema` is for vLLM structured output. It includes action and clarification fields, but no `operations`.
- `domain_candidate_internal_schema` is the completed domain candidate shape sent to `/intent/normalize`.
- `intent_patch_internal_schema` is the internal JSON Patch-compatible shape used by Python validation.
- n8n must not create `operations`; it sends the candidate to `/intent/normalize`.

## Milestone 3.5: LLM Evaluation

The evaluation harness measures the vLLM Intent Generator before the release workflow is added. It uses `tests/llm_eval/operator_intents.jsonl`, calls vLLM with the clarification-aware structured output schema, runs deterministic pre-checks over `intent_text`, normalizes through Python only for `PROPOSE_PATCH`, then validates through Python. It never calls `/patch/apply`.

Start the TRT API:

```powershell
uvicorn trt_core.api:app --host 0.0.0.0 --port 8000
```

Run the evaluation:

```powershell
python scripts/evaluate_vllm_intent_generator.py
```

Optional environment variables:

```text
VLLM_BASE_URL=http://192.168.50.168:27783/v1
VLLM_MODEL=cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit
TRT_API_BASE_URL=http://localhost:8000
VLLM_API_KEY=EMPTY
```

Outputs:

- `reports/llm_eval_results.jsonl`: one record per operator intent case.
- `reports/llm_eval_summary.json`: aggregate metrics including parse rate, finish reason rates, schema validity, normalization success, validation success, expected-valid agreement, exact field matches, clarification detection, latency, token use, and failure breakdown.

The evaluator flow is:

```text
operator intent -> deterministic pre-check -> vLLM action/candidate -> /intent/normalize -> /patch/validate
```

The evaluator rejects `finish_reason=length` because that indicates the model may have returned incomplete JSON. It also rejects `NEEDS_CLARIFICATION`, `UNSUPPORTED_REQUEST`, and deterministic pre-check failures before normalization.

Milestone 3.6 target metrics:

- `false_accept_rate <= 0.10`
- `invalid_rejection_rate >= 0.80`
- `valid_accept_rate >= 0.90`

## Milestone 5: Supervisor Reconciliation

The Supervisor reads the current released TRT plus production-line State Records and produces a Reconciliation Plan. It never edits TRT versions and does not call an LLM, ROS, Omniverse, ScenarioSpecs, or RunArtifacts.

Decision values:

- `IMMEDIATE_SWITCH`
- `WAIT_FOR_CHECKPOINT`
- `DEGRADED_SWITCH`
- `REJECT_INCOMPATIBLE`
- `NO_CHANGE`

Typical API flow:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/state/update `
  -ContentType "application/json" `
  -InFile tests/fixtures/state_records_running_with_wip.json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/supervisor/reconcile `
  -ContentType "application/json" `
  -Body '{}'
```

Plans are stored in `data/reconciliation_plans/` and include `source_state_hash` and `source_trt_hash` for traceability.

## Milestone 6: ScenarioSpec Generation

The governance workspace can now generate Isaac-adapter-compatible `ScenarioSpec` JSON files without importing Isaac Sim modules or running simulation code. The output is written through file exchange only under `outputs/scenario_specs/`; the digital twin writes results under `outputs/run_artifacts/`.

Generation inputs:

- released TRT version
- State Records
- Reconciliation Plan
- scenario template registry

The generator refuses `REJECTED` reconciliation plans, rejects Isaac-incompatible `ASK_OPERATOR` strategies, and always preserves implicit runtime entanglement semantics. It does not emit `event_injections`, manual event lists, or predefined entanglement timestamps.

Default output paths in generated ScenarioSpecs are relative:

- `outputs/scenario_specs/<scenario_spec_id>.json`
- `outputs/run_artifacts/<scenario_spec_id>_run_artifact.json`

Example API call:

```powershell
curl -X POST http://127.0.0.1:8000/scenario/generate `
  -H "Content-Type: application/json" `
  -d "{\"release_id\":\"rel_001\",\"trt_id\":\"trt-demo\",\"trt_version\":\"v1\",\"reconciliation_plan_id\":\"rec_ready_001\",\"scenario_template_id\":\"surgical_sorting_v1\"}"
```

Expected generated response shape:

```json
{
  "status": "GENERATED",
  "scenario_spec_id": "scn_...",
  "scenario_spec_path": "outputs/scenario_specs/scn_....json"
}
```

Relevant files:

- `scenario_generation/`
- `schemas/scenario_spec.schema.json`
- `schemas/scenario_template_registry.schema.json`
- `tests/fixtures/scenario_templates.json`

Run the tests:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_scenario_generation.py tests\test_scenario_template_registry.py tests\test_scenario_export.py
```

## Scope

Implemented:

- TRT and Intent Patch JSON Schemas
- validation firewall
- JSON Patch operations `test`, `add`, `replace`, and `remove`
- rejection of `move` and `copy`
- read-only `state` fields
- base version freshness checks
- semantic consistency checks
- file-backed TRT version repository
- Audit Bundle generation for accepted and rejected patches
- minimal FastAPI integration surface for future n8n workflows
- Supervisor state reconciliation and Reconciliation Plan generation
- ScenarioSpec generation and file export for the Isaac Sim adapter boundary

Not implemented in this milestone:

- LLM calls
- speech recognition
- Omniverse
- ROS
- digital twin simulation
- RunArtifacts
