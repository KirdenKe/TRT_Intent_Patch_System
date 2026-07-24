# Start The Isaac Host Runner

The correct execution path is:

```text
Docker trt-api -> Windows host runner -> Isaac Sim python.bat -> pick_up_example.py
```

The Docker container must not try to execute the Windows Isaac Sim path directly.

## Fresh Clone Bootstrap

From a newly cloned repository, run these steps from Windows PowerShell in the project root.

### 1. Create the local Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest
```

The editable install provides both the TRT API package and the host-runner dependencies used by `host_isaac_runner_service.py`.

### 2. Create the Docker containers

Create `.env` next to `docker-compose.yml` so Docker can reach the Windows host runner:

```text
ISAAC_HOST_RUNNER_URL=http://host.docker.internal:8765
```

If you need to override the project path through Compose, add `HOST_PROJECT_ROOT=<absolute Windows clone path>`. When the path contains a literal `$`, prefer `data/isaac_host_config.json`; otherwise escape `$` as `$$` in `.env` or Compose values.

Build and start the services:

```powershell
docker compose build trt-api
docker compose up -d trt-api
Invoke-RestMethod http://localhost:8000/health
```

To start n8n from a fresh clone, first make sure the `n8n` volume host path in `docker-compose.yml` points to a real local n8n data directory for your machine. Then run `docker compose up -d n8n`.

Use `docker compose up -d --force-recreate trt-api` after changing `.env` or `docker-compose.yml`; `docker compose restart` keeps the old container environment.

### 3. Configure host_runner paths

Edit `data/isaac_host_config.json` for the cloned machine:

```json
{
  "host_project_root": "C:\\path\\to\\trt_intent_patch_system",
  "container_project_root": "/app",
  "isaac_working_directory": "C:\\Dev\\IsaacSim",
  "python_bat": "C:\\Dev\\IsaacSim\\_build\\windows-x86_64\\release\\python.bat",
  "entry_script": "C:\\Dev\\IsaacSim\\_build\\windows-x86_64\\release\\standalone_examples\\api\\isaacsim.robot.manipulators\\ur5\\pick_up_example.py",
  "seed_db_path": "C:\\Dev\\IsaacSim\\_build\\windows-x86_64\\release\\standalone_examples\\api\\isaacsim.robot.manipulators\\ur5\\tasks\\seed_sweep.sqlite3"
}
```

Start the host runner:

```powershell
.\scripts\start_host_isaac_runner.ps1 -Port 8765
```

In another PowerShell window, recreate and verify `trt-api`:

```powershell
docker compose up -d --force-recreate trt-api
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://localhost:8000/debug/isaac-host-runner-status
.\scripts\check_isaac_host_runner_from_container.ps1
```

Expected high-level status:

```text
status = OK
host_runner_url_configured = true
available = true
```

## 1. Start The Host Runner

Open Windows PowerShell.

```powershell
cd C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system
.\scripts\start_host_isaac_runner.ps1 -Port 8765
```

The service defaults to:

```text
http://127.0.0.1:8765
```

From the Windows host, check:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

Expected shape:

```json
{
  "status": "OK",
  "service": "host_isaac_runner",
  "python_bat_exists": true,
  "entry_script_exists": true,
  "working_directory_exists": true
}
```

## 2. Configure host paths

Set the host runner URL for Docker. With Docker Desktop, `host.docker.internal` points from the Linux container to the Windows host.

Documentation is not runtime configuration. The API reads Windows host paths from `data/isaac_host_config.json` so paths containing a literal `$` do not depend on Docker Compose interpolation.

For this project, use `data/isaac_host_config.json` because the Windows username contains `$`:

```json
{
  "host_project_root": "C:\\Users\\$93I000-7RFCRA0J9IC9\\Documents\\Docker\\n8n_data\\trt_intent_patch_system",
  "container_project_root": "/app",
  "isaac_working_directory": "C:\\Dev\\IsaacSim",
  "python_bat": "C:\\Dev\\IsaacSim\\_build\\windows-x86_64\\release\\python.bat",
  "entry_script": "C:\\Dev\\IsaacSim\\_build\\windows-x86_64\\release\\standalone_examples\\api\\isaacsim.robot.manipulators\\ur5\\pick_up_example.py",
  "seed_db_path": "C:\\Dev\\IsaacSim\\_build\\windows-x86_64\\release\\standalone_examples\\api\\isaacsim.robot.manipulators\\ur5\\tasks\\seed_sweep.sqlite3"
}
```

`seed_db_path` is the database-mode layout/seed input DB for `pick_up_example.py`. It is not the per-run KPI/result database. The per-run output DB remains under `outputs/run_artifacts/sim_*.sqlite`.

## 3. Configure trt-api

Do not use `docker compose restart trt-api` after changing Compose environment variables. `restart` keeps the existing container environment. Recreate the container so Compose interpolates the new values.

Option A, recommended: create a `.env` file next to `docker-compose.yml` for Docker-only values:

```text
ISAAC_HOST_RUNNER_URL=http://host.docker.internal:8765
```

Then run:

```powershell
docker compose config
docker compose up -d --force-recreate trt-api
```

Option B: set PowerShell environment variables and recreate the container in the same terminal:

```powershell
$env:ISAAC_HOST_RUNNER_URL = "http://host.docker.internal:8765"
docker compose config
docker compose up -d --force-recreate trt-api
```

If `host.docker.internal` is not available in your environment, use the Windows host IP address instead:

```powershell
$env:ISAAC_HOST_RUNNER_URL = "http://<windows-host-ip>:8765"
docker compose config
docker compose up -d --force-recreate trt-api
```

If you choose to use `HOST_PROJECT_ROOT` as an environment override, escape `$` as `$$` in Compose. The safer path for this workspace is `data/isaac_host_config.json`.

## 4. Check From trt-api

After recreating `trt-api`, call:

```powershell
Invoke-RestMethod http://localhost:8000/debug/isaac-host-runner-status
```

This endpoint reports:

- `ISAAC_EXECUTION_MODE`
- whether `ISAAC_HOST_RUNNER_URL` is configured
- whether the host runner is reachable
- whether `python.bat`, `pick_up_example.py`, and `C:\Dev\IsaacSim` exist on the host
- the host project root source: `config_file` or `env`
- the sample container ScenarioSpec path and mapped Windows host path

Expected:

```text
status = OK
host_runner_url_configured = true
available = true
```

## Startup Verification Checklist

A. Start the host runner on Windows:

```powershell
python -m uvicorn host_isaac_runner_service:app --host 0.0.0.0 --port 8765
```

B. Test from the Windows host:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

C. Recreate `trt-api`:

```powershell
docker compose up -d --force-recreate trt-api
```

D. Test from inside the container:

```powershell
docker compose exec trt-api python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:8765/health', timeout=3).read().decode())"
```

E. Test backend status:

```powershell
Invoke-RestMethod http://localhost:8000/debug/isaac-host-runner-status
```

## 5. Dry-Run The Isaac Command

Use the host runner dry-run endpoint before launching Isaac:

```powershell
$body = @{
  scenario_spec_id = "scn_preview"
  scenario_spec_path = "C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system\outputs\scenario_specs\scn_preview.json"
  output_db_path = "C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system\outputs\run_artifacts\sim_preview.sqlite"
  run_id = "sim_preview"
  command_args = @{
    num_envs = 4
    headless = $false
    global_seed = $null
    layout_source = "auto"
    episode_success_requires_reset_cycles = 1
    allowed_overlap_ratio = 0.99
    chosen_intervention_mode = "continue-until-arrival"
    travel_time = 5
    fix_duration = 8
    resume_delay = 0.5
    add_reference_number = 27
    reuse_verified_seed = $true
    seed_db_path = "C:\Dev\IsaacSim\_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5\tasks\seed_sweep.sqlite3"
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/isaac/dry-run -ContentType "application/json" -Body $body
```

The dry-run command should be equivalent to:

```text
C:\Dev\IsaacSim\_build\windows-x86_64\release\python.bat
C:\Dev\IsaacSim\_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5\pick_up_example.py
--num_envs 4
--headless false
--layout_source auto
--episode_success_requires_reset_cycles 1
--allowed_overlap_ratio 0.99
--chosen_intervention_mode continue-until-arrival
--travel_time 5
--fix_duration 8
--resume_delay 0.5
--add_reference_number 27
--run_id sim_preview
--output_db_path C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system\outputs\run_artifacts\sim_preview.sqlite
--seed_db_path C:\Dev\IsaacSim\_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5\tasks\seed_sweep.sqlite3
--reuse_verified_seed
```

`--global_seed`, `--max_seed_trials`, and `--reuse_precomputed_layouts` are omitted by default. `--seed_db_path` is only passed from host config when a non-empty host-visible path is configured and exists. If it is missing, the dry-run reports a warning and omits the flag. `--output_db_path` is the per-run KPI/result database and must be different from `seed_sweep.sqlite3`.

## 6. Test /simulation/run

Once `/debug/isaac-host-runner-status` is OK, call:

```powershell
$body = @{
  scenario_spec_id = "scn_preview"
  scenario_spec_path = "outputs/scenario_specs/scn_preview.json"
  run_mode = "SYNC"
  headless = $false
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://localhost:8000/simulation/run -ContentType "application/json" -Body $body
```

If the host runner URL is missing, `/simulation/run` returns a controlled setup error with instructions instead of attempting to run Isaac from Docker.

If Isaac exits with return code `0` but does not create the per-run SQLite file, `/simulation/run` returns `SIMULATION_COMPLETED_BUT_RESULT_DB_MISSING`. That means Isaac launched and shut down cleanly, but `pick_up_example.py` did not satisfy the result artifact contract. The response includes both the seed DB input path and the expected output DB path so they are not confused.

## 7. Manual Result DB Smoke Test

From Windows PowerShell, run the entry point directly with both the seed DB input path and a separate result DB output path:

```powershell
cd C:\Dev\IsaacSim

.\_build\windows-x86_64\release\python.bat `
  .\_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5\pick_up_example.py `
  --num_envs 4 `
  --headless false `
  --layout_source auto `
  --episode_success_requires_reset_cycles 1 `
  --allowed_overlap_ratio 0.99 `
  --chosen_intervention_mode continue-until-arrival `
  --travel_time 5 `
  --fix_duration 8 `
  --resume_delay 0.5 `
  --add_reference_number 27 `
  --run_id manual_test `
  --output_db_path C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system\outputs\run_artifacts\manual_test.sqlite `
  --seed_db_path C:\Dev\IsaacSim\_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5\tasks\seed_sweep.sqlite3 `
  --reuse_verified_seed

Test-Path C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system\outputs\run_artifacts\manual_test.sqlite
```

Expected: `Test-Path` returns `True`, and the SQLite file contains `simulation_runs`, `line_kpis`, and `tool_events`.

Inspect finalization from Windows PowerShell:

```powershell
sqlite3 "C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system\outputs\run_artifacts\manual_test.sqlite" "select run_id,status,completed_at,error_message from simulation_runs;"
sqlite3 "C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system\outputs\run_artifacts\manual_test.sqlite" "select count(*) from line_kpis;"
sqlite3 "C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system\outputs\run_artifacts\manual_test.sqlite" "select * from line_kpis limit 5;"
```

Expected:

- `simulation_runs.status = COMPLETED`
- `completed_at` is not null
- `line_kpis` count is at least `--num_envs`
