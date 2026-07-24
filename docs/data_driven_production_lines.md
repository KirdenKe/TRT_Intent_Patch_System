# Data-Driven Production Lines

## Fresh Clone Setup

After cloning the repository, create the local Python environment first:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest
```

Create the Docker containers from the repository root:

```powershell
docker compose build trt-api
docker compose up -d trt-api
Invoke-RestMethod http://localhost:8000/health
```

The current Docker execution mode is `host_runner`. Configure it before running Isaac-backed simulation endpoints:

1. Copy or edit `data/isaac_host_config.json` so `host_project_root`, `python_bat`, `entry_script`, and `seed_db_path` match the cloned machine.
2. Create `.env` next to `docker-compose.yml` with `ISAAC_HOST_RUNNER_URL=http://host.docker.internal:8765`.
3. Start the Windows host runner with `.\scripts\start_host_isaac_runner.ps1 -Port 8765`.
4. Recreate `trt-api` with `docker compose up -d --force-recreate trt-api`.
5. Verify with `Invoke-RestMethod http://localhost:8000/debug/isaac-host-runner-status`.

See `docs/start_host_isaac_runner.md` for the full host-runner checklist.

`data/production_lines/line_registry.json` is the source of truth for production-line topology.
It defines which lines exist, whether they are enabled, and how each line maps to robot,
workspace, tray, and simulation paths.

Responsibilities:

- `line_registry.json`: production-line topology and digital-twin binding data.
- TRT files: task requirements and policy for the enabled lines.
- `current_state.json`: runtime state for the enabled lines.
- Scenario templates: generic scene behavior and simulation parameters.
- n8n workflows: orchestration only; they do not own line topology.

ScenarioSpec generation resolves line bindings from the line registry. Templates such as
`surgical_sorting_data_driven_v1` no longer hardcode `line_1` through `line_4` as the
source of truth.

Adding a line:

1. Add the line entry to `data/production_lines/line_registry.json`.
2. Regenerate TRT and runtime state with `scripts/generate_ent_demo_state.py`.
3. Run the data-driven line registry tests.

No n8n workflow JSON or API source-code change should be required when changing from
four to five lines, assuming the registry and generated data files are updated.
