# UR5 Isaac Sim Script Inventory

## Fresh Clone Setup

Create the repository-local Python environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest tests\test_digital_twin_adapter.py
```

Create and start the Docker API container:

```powershell
docker compose build trt-api
docker compose up -d trt-api
Invoke-RestMethod http://localhost:8000/health
```

The current integration boundary is `Docker trt-api -> Windows host_runner -> Isaac Sim python.bat -> pick_up_example.py`. Configure it for a new clone:

1. Edit `data/isaac_host_config.json` so it points to the cloned repository, Isaac Sim working directory, `python.bat`, UR5 `pick_up_example.py`, and optional `seed_sweep.sqlite3`.
2. Create `.env` next to `docker-compose.yml`:

```text
ISAAC_HOST_RUNNER_URL=http://host.docker.internal:8765
```

3. Start the host service:

```powershell
.\scripts\start_host_isaac_runner.ps1 -Port 8765
```

4. Recreate and check the API container:

```powershell
docker compose up -d --force-recreate trt-api
Invoke-RestMethod http://localhost:8000/debug/isaac-host-runner-status
```

Use `docs/start_host_isaac_runner.md` for the expanded checklist and dry-run examples.

Inspected folder:
`C:\Dev\IsaacSim\_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5`

## Entry Point

- `pick_up_example.py`
  - Actual Isaac Sim entry point.
  - Creates the `SimulationApp`.
  - Parses runtime arguments.
  - Builds the run configuration.
  - Creates `World`.
  - Creates the `PickPlaceMultiTool` task.
  - Initializes `PickPlaceController` instances.
  - Runs the pick/place simulation loop.
  - Persists the existing seed-sweep run result to `tasks/seed_sweep.sqlite3`.
  - This is the host-runner entry script, but the host runner should pass only
    command-compatible `pick_up_example.py` arguments such as `--num_envs`,
    `--layout_source`, `--allowed_overlap_ratio`, `--add_reference_number`,
    and boolean seed/layout flags.
  - Do not pass ScenarioSpec-only orchestration arguments such as
    `--scenario-spec`, `--run-id`, `--output-db`, `--max-steps`, or
    `--affected-lines` unless the Isaac entry point is intentionally changed
    and manually verified.

## Helpers And Tasks

- `tasks/tools_classification.py`
  - Helper module used by tooling layout/classification code.
  - Must not be a standalone runner.
  - Should expose helper APIs such as `get_tools_classification_from_scenario(scenario_spec, line_id)`.
  - Maintains per-line wanted/unwanted tooling derived from ScenarioSpec.

- `tasks/tool_layout.py`
  - Defines packing/layout helpers and `SeedSweepDB`.
  - Imports `get_tools_classification` from `tasks.tools_classification`.
  - Existing classification call was URL-based; ScenarioSpec-derived data should
    be bridged through the existing database/layout path or another verified
    helper interface, not by turning this helper into a runner.

- `tasks/pick_place.py`
  - Defines `PickPlaceSingleTool` and `PickPlaceMultiTool`.
  - Owns scene/task object creation for tooling and robots.
  - Provides runtime helpers used by `pick_up_example.py`, including sensors, tooling IDs, layout records, and entanglement handling.

- `controller/pick_place.py`
  - Pick/place controller implementation.
  - Converts pick and place targets into robot actions.

- `controller/timed_arrival.py`
  - Operator intervention scheduling and entanglement runtime handling.

- `validation/assertions.py`
  - Existing validation of run artifacts produced by the Isaac-side runtime.

## Existing Isaac-Side Adapter

- `digital_twin_adapter/run_from_scenario.py`
  - Wrapper that reads ScenarioSpec and calls `pick_up_example.py`.
  - Useful reference, but Milestone 9 uses `pick_up_example.py` as the direct host-runner entry script.

- `digital_twin_adapter/scenario_adapter.py`
  - Converts ScenarioSpec fields into existing `pick_up_example.py` arguments.

- `digital_twin_adapter/run_artifact.py`
  - Builds JSON RunArtifact documents from the existing run result.

## Integration Boundary

- Dockerized `trt-api` should not execute Windows Isaac paths directly.
- `trt-api` calls a Windows-host service.
- The Windows-host service launches `pick_up_example.py`.
- For the current execution contract, the host service maps ScenarioSpec/config
  into the original `pick_up_example.py` CLI arguments and does not pass
  unsupported ScenarioSpec-only args.
- `tools_classification.py` remains helper-only.
