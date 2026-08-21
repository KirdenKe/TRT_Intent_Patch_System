# Start the TRT System and Isaac Host Runner

The runtime crosses three environments:

```text
Operator -> n8n -> Docker trt-api -> Windows host runner
         -> configured vLLM              -> Isaac Sim python.bat
                                           -> pick_up_example.py
```

The vLLM servers and Isaac Sim project are external dependencies. A new
machine must configure their locations before starting the system. Docker must
not attempt to execute a Windows Isaac Sim path directly.

## Handover Configuration Map

| Setting | Authoritative file or location | When to change it |
| --- | --- | --- |
| Production vLLM endpoint and model used by `trt-api` | Project-root `.env`, copied from `.env.example`: `VLLM_CHAT_COMPLETIONS_URL`, `VLLM_MODEL` | The production model, host, or port changes |
| Direct n8n vLLM calls | `n8n_workflows/chat_operator_task_allocation.workflow.json`, node `vLLM Format User Response`; `n8n_workflows/intent_to_patch_review.workflow.json`, nodes `LLM Generate Intent Patch` and `Retry LLM Generate Intent Patch` | Keep these nodes synchronized with the production model and endpoint, then re-import and publish the workflows |
| TC7 comparison models | `tools/llm_generation_benchmark.py`, constant `MODELS` | A benchmark model or endpoint changes |
| Windows project and Isaac paths | `data/isaac_host_config.json` | The repository, Isaac installation, entry script, or seed database moves |
| Docker-to-host runner URL | Project-root `.env`: `ISAAC_HOST_RUNNER_URL` | Hostname, IP address, or runner port changes |
| n8n data directory | Project-root `.env`: `N8N_DATA_DIR` | n8n is installed in a different host directory |

The Python and PowerShell source files contain conspicuous `HANDOVER
CONFIGURATION` comments beside their fallback values. Configure the files in
the table instead of customizing those fallback constants.

## Current vLLM Endpoints

| Purpose | Model | Endpoint |
| --- | --- | --- |
| Production default | `cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit` | `http://192.168.50.168:26615/v1/chat/completions` |
| TC7 benchmark | `Qwen/Qwen3.6-35B-A3B-FP8` | `http://192.168.50.168:21909/v1/chat/completions` |
| TC7 benchmark | `meta-llama/Llama-3.1-8B-Instruct` | `http://192.168.50.168:22530/v1/chat/completions` |

Changing `.env` updates `trt-api` after the container is recreated. It does
not rewrite an already published n8n workflow. When the production model
changes, update the checked-in n8n nodes listed above, import them into n8n,
and publish the updated parent and sub-workflows.

## 1. Prepare a Fresh Clone

From Windows PowerShell in the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
python -m pytest tests/test_chat_sessions.py `
  tests/test_strategy_selection.py `
  tests/test_digital_twin_adapter.py `
  tests/test_n8n_strategy_generation_failure.py `
  tests/test_m12_runner_strategy_selection.py -q
```

These focused startup and integration tests currently pass. The complete test
suite also includes older structural assertions in `tests/test_n8n_workflows.py`
that have not yet been rewritten for the current multi-candidate and
session-aware workflows. See **Future Tasks** below before interpreting a full
`pytest` result.

Create the local environment file:

```powershell
Copy-Item .env.example .env
```

Edit `.env` for the receiving machine. At minimum, verify:

```text
VLLM_CHAT_COMPLETIONS_URL=http://192.168.50.168:26615/v1/chat/completions
VLLM_MODEL=cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit
ISAAC_HOST_RUNNER_URL=http://host.docker.internal:8765
N8N_DATA_DIR=C:/path/to/n8n_data
```

Do not commit `.env`; it is ignored by Git.

## 2. Configure Windows and Isaac Paths

Edit `data/isaac_host_config.json` and replace every machine-specific path:

```json
{
  "host_project_root": "C:\\path\\to\\trt_intent_patch_system",
  "container_project_root": "/app",
  "isaac_working_directory": "C:\\path\\to\\IsaacSim",
  "python_bat": "C:\\path\\to\\IsaacSim\\_build\\windows-x86_64\\release\\python.bat",
  "entry_script": "C:\\path\\to\\IsaacSim\\_build\\windows-x86_64\\release\\standalone_examples\\api\\isaacsim.robot.manipulators\\ur5\\pick_up_example.py",
  "seed_db_path": "C:\\path\\to\\IsaacSim\\_build\\windows-x86_64\\release\\standalone_examples\\api\\isaacsim.robot.manipulators\\ur5\\tasks\\seed_sweep.sqlite3"
}
```

`host_project_root` maps container artifact paths such as
`/app/outputs/scenario_specs/...` to the Windows clone. `seed_db_path` is a
layout input database; it is not the per-run result database under
`outputs/run_artifacts/`.

The launcher reads this JSON automatically. Command-line parameters remain
available as one-time overrides:

```powershell
.\scripts\start_host_isaac_runner.ps1 `
  -WorkingDirectory "D:\IsaacSim" `
  -PythonBat "D:\IsaacSim\_build\windows-x86_64\release\python.bat" `
  -EntryScript "D:\IsaacSim\...\pick_up_example.py"
```

Prefer the JSON file for a persistent installation. It also avoids Docker
Compose interpolation problems when a Windows username contains `$`.

## 3. Start and Verify the Host Runner

Start it outside Docker:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\start_host_isaac_runner.ps1 -Port 8765
```

The launcher binds to `0.0.0.0` by default so both Windows and Docker Desktop
can reach it. It performs a strict preflight before opening the service and
stops with actionable errors when `host_project_root`, `python.bat`, the Isaac
working directory, or `pick_up_example.py` still points to another machine.

Starting this script starts the HTTP host-runner service only. It intentionally
does **not** launch Isaac Sim immediately. Isaac Sim starts when `trt-api` or a
diagnostic client sends an accepted `POST /isaac/run` or `POST /isaac/runs`
request. A visible `/isaac/run` route therefore proves that the bridge is
listening; it does not prove that an Isaac subprocess has been requested.

One approved operator intent generates three candidate strategies by default:
one deterministic operator-faithful baseline and two exploratory candidates.
The strategy worker evaluates them strictly sequentially, so the normal host
access log contains exactly three `POST /isaac/run` entries unless
`candidate_count` was explicitly changed. This is one request per candidate,
not the same candidate being retried three times.

The startup output prints the configuration file, resolved Isaac paths,
Windows health URL, and Docker target URL. Keep this PowerShell process open.
In another PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

Expected fields:

```text
status = OK
ready = true
python_bat_exists = true
entry_script_exists = true
working_directory_exists = true
bind_address = 0.0.0.0
```

`status = MISCONFIGURED` or any false path check means that the service must
not accept an Isaac launch. Correct `data/isaac_host_config.json` before
starting Docker. Do not continue with a fallback path that belongs to another
machine.

## 4. Start Docker Services

Build and start `trt-api`:

```powershell
docker compose config
docker compose build trt-api
docker compose up -d --force-recreate trt-api
Invoke-RestMethod http://127.0.0.1:8000/health
```

Recreate rather than restart after changing `.env`; `docker compose restart`
retains the old container environment.

Verify Docker can reach the Windows process:

```powershell
.\scripts\check_isaac_host_runner_from_container.ps1
Invoke-RestMethod http://127.0.0.1:8000/debug/isaac-host-runner-status
```

Expected high-level result:

```text
status = OK
host_runner_url_configured = true
available = true
```

Then start n8n:

```powershell
docker compose up -d n8n
```

Import the workflow JSON files under `n8n_workflows/` into a new n8n instance
and publish the parent workflow and referenced sub-workflows. Files in
`n8n_exports/` are snapshots for audit/recovery; editing a snapshot does not
change the live n8n database.

## 5. Verify vLLM Connectivity

Test the production endpoint from Windows:

```powershell
$body = @{
  model = "cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit"
  messages = @(@{ role = "user"; content = "Reply with OK." })
  max_tokens = 8
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post `
  -Uri "http://192.168.50.168:26615/v1/chat/completions" `
  -ContentType "application/json" `
  -Body $body
```

For a new server, verify that `/v1/models` reports the same model identifier
used in `.env` and the n8n request bodies. A reachable port with a mismatched
model name still causes request failures.

## 6. Dry-Run the Isaac Command

Before launching Isaac, use the host-runner dry-run endpoint:

```powershell
$root = (Resolve-Path .).Path
$body = @{
  scenario_spec_id = "scn_preview"
  scenario_spec_path = "$root\outputs\scenario_specs\scn_preview.json"
  output_db_path = "$root\outputs\run_artifacts\sim_preview.sqlite"
  run_id = "sim_preview"
  command_args = @{
    num_envs = 4
    headless = $false
    global_seed = 65
    max_seed_trials = 1
    reuse_precomputed_layouts = $true
    layout_source = "auto"
    episode_success_requires_reset_cycles = 1
    allowed_overlap_ratio = 0.99
    chosen_intervention_mode = "immediate-stop"
    travel_time = 1.0
    fix_duration = 3.0
    resume_delay = 1.0
    add_reference_number = 5
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/isaac/dry-run `
  -ContentType "application/json" `
  -Body $body
```

Confirm that the returned command uses the receiving machine's `python.bat`,
`pick_up_example.py`, project path, and result database path. The current
last-resort simulation defaults shown above are not a substitute for values
explicitly compiled into a ScenarioSpec.

The dry-run endpoint validates and displays the command but does not launch
Isaac. A real run response contains:

```text
process_started = true
pid = <Windows process ID>
launch_method = DIRECT
actual_launch_command = [...python.bat, ...pick_up_example.py, ...]
```

The runner launches the same `python.bat` argument vector used by the known-good
manual PowerShell command. If Windows rejects direct batch process creation, it
retries once through `COMSPEC` and records `launch_method = COMSPEC_FALLBACK`.
The per-run `stdout_path` and `stderr_path` identify the logs to inspect if the
process starts and then exits.

List every request retained by the current host-runner process:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/isaac/runs |
  ConvertTo-Json -Depth 8
```

For each run, distinguish `launch_attempted` from `process_started`. The host
runner can return HTTP 200 with `status = FAILED` for a request rejected before
launch, so the HTTP access log alone is not evidence that Isaac Sim started.
Run the checker again after the candidate batch; running it only before the
operator workflow naturally reports no recent runs.

## 7. Troubleshooting

### `trt-api` cannot reach the host runner

- Current launchers listen on `0.0.0.0` by default. For an older checkout or an
  explicit override, start it as follows:

  ```powershell
  .\scripts\start_host_isaac_runner.ps1 -HostAddress 0.0.0.0 -Port 8765
  ```

- If `host.docker.internal` is unavailable, set
  `ISAAC_HOST_RUNNER_URL=http://<windows-host-ip>:8765` in `.env`, then recreate
  `trt-api`.

- Allow inbound TCP port 8765 in Windows Firewall for the applicable network
  profile. A local `127.0.0.1` health check can pass while Docker remains
  blocked by the firewall.

- Run `scripts/check_isaac_host_runner_from_container.ps1`. It checks Windows,
  recent host-runner requests, the Docker boundary, `trt-api` health, and the
  backend debug route in that order. It reports the HTTP error body and relevant
  recovery commands instead of emitting an unexplained Python traceback.

### Windows cannot reach `trt-api` on port 8000

The Docker health check runs inside the container. Therefore, a container can
be marked `healthy` even when Windows-to-container port forwarding is blocked.
Use the numeric loopback address first to avoid `localhost` proxy or name
resolution behavior:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
curl.exe --noproxy "*" http://127.0.0.1:8000/health
Test-NetConnection 127.0.0.1 -Port 8000
```

If the checker reports that the API is healthy inside its container but the
Windows request times out, n8n-to-`trt-api` and `trt-api`-to-host-runner traffic
can still operate on the Compose network. The remaining problem is limited to
the published Windows port. Check proxy environment variables, VPN/security
software, Windows Firewall, and Docker Desktop port forwarding. The absence of
a Windows request in `docker compose logs trt-api` confirms that the request
never reached Uvicorn.

### The route exists but Isaac Sim does not start

1. Read `GET http://127.0.0.1:8765/health`. It must report `status = OK` and
   `ready = true`.
2. Confirm that a real `POST /isaac/run` or `POST /isaac/runs` occurred. Starting
   the host service and calling `/isaac/dry-run` never launch Isaac.
3. Inspect the run response. `process_started = false` means preflight or
   process creation failed; read `errors` and `missing_paths`.
4. If `process_started = true`, inspect `pid`, `return_code`, `stdout_path`, and
   `stderr_path`. An immediate nonzero return code is an Isaac/script failure,
   not a connectivity failure.
5. Verify that `scenario_spec_path` exists on Windows. A stale
   `host_project_root` can make the API generate a path from the previous
   machine, in which case the runner rejects the request before launching.
6. Read the explicit console event printed before each access-log line:
   `isaac_run.rejected_before_launch`, `isaac_run.process_started`, or
   `isaac_run.finished`. Three bare HTTP 200 lines are not sufficient evidence
   of three successful launches.

### Isaac reaches `app ready` but simulation does not begin

This symptom is distinct from a missing path or failed process creation: Isaac
has started, but the Python workload has not advanced past application startup.
First copy `actual_launch_command` from `GET /isaac/runs` and confirm the same
argument vector succeeds when entered manually.

The host runner uses `launch_method = DIRECT` by default to match that manual
execution. Older revisions forced every batch through an additional
`cmd.exe /c` wrapper; that wrapper could change batch-process behavior on some
Isaac installations. Update and restart the host runner before retesting. A
`COMSPEC_FALLBACK` is now used only when direct process creation itself fails.

If the updated direct launch still stalls, preserve the run's `stdout_path`,
`stderr_path`, `actual_launch_command`, working directory, and `command_args`.
Those fields are required to compare the service launch with the successful
manual launch; an `app ready` screenshot alone does not identify the boundary.

### Compose warns that `HOST_PROJECT_ROOT` is not set

Current Compose configuration no longer interpolates `HOST_PROJECT_ROOT`.
`data/isaac_host_config.json` is authoritative because `$` in Windows usernames
can be altered by Compose interpolation. If this warning still appears, update
the checkout and recreate `trt-api`; do not add a blank variable merely to hide
the warning.

### Isaac exits without a result database

`SIMULATION_COMPLETED_BUT_RESULT_DB_MISSING` means Isaac returned exit code 0
but `pick_up_example.py` did not create the requested per-run SQLite artifact.
Check the entry-script version and confirm it supports `--run_id` and
`--output_db_path`.

### vLLM requests fail after a model change

Verify all three layers:

1. `.env` production model and URL.
2. Direct n8n URL/model values in the two checked-in workflow files.
3. `MODELS` in `tools/llm_generation_benchmark.py` for TC7 only.

After changing n8n files, re-import and publish them. After changing `.env`,
recreate `trt-api`.

## 8. Future Tasks

### Align n8n workflow tests with the current architecture

**Status:** Pending maintenance work. This does not mean that 22 live workflow
executions failed. It means that 22 automated assertions in
`tests/test_n8n_workflows.py` still expect nodes, connections, or prompt text
from an older workflow design.

This is sometimes called **test debt**: the application has evolved, but part
of its automated test code has not yet been updated to describe the intended
current behavior. Until this work is completed, the full test-suite result can
mix genuine regressions with failures caused by obsolete expectations.

Required work:

1. Replace assertions for removed single-scenario nodes with assertions for
   candidate-batch generation, sequential candidate simulation, evidence
   gating, ranking, and selected-candidate handling.
2. Update chat-routing assertions for session loading, global cancellation,
   required-field continuation, release approval, and deployment decisions.
3. Update prompt assertions to match the current policy of omitting explicit
   sampling controls and using the current prompt wording.
4. Preserve endpoint checks for the production vLLM URL and model, without
   coupling unrelated workflow tests to a machine-specific port where an
   environment override is supported.
5. Add checks that the checked-in workflow sources and published n8n workflow
   have equivalent critical nodes, connections, model identifiers, and
   endpoints.
6. Run the complete suite and manually inspect every remaining failure instead
   of automatically treating it as an application defect.

Completion criteria:

- `tests/test_n8n_workflows.py` describes the current published workflow rather
  than retired nodes or connections.
- Every workflow assertion has a clear behavioral purpose.
- The complete test suite passes, or any remaining failure is documented as a
  reproducible current-system defect.
- The focused 79-test backend and adapter regression set continues to pass.
