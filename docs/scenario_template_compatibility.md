# Scenario Template Compatibility

## Fresh Clone Setup

Set up a local Python environment and run the template tests:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest tests\test_scenario_generation.py tests\test_scenario_template_registry.py tests\test_scenario_export.py
```

Build and run the Docker API container:

```powershell
docker compose build trt-api
docker compose up -d trt-api
Invoke-RestMethod http://localhost:8000/health
```

When a generated ScenarioSpec will be sent to Isaac Sim, configure `host_runner`:

1. Set machine-specific paths in `data/isaac_host_config.json`.
2. Add `.env` with `ISAAC_HOST_RUNNER_URL=http://host.docker.internal:8765`.
3. Start `.\scripts\start_host_isaac_runner.ps1 -Port 8765`.
4. Recreate `trt-api` with `docker compose up -d --force-recreate trt-api`.
5. Confirm `Invoke-RestMethod http://localhost:8000/debug/isaac-host-runner-status` reports `available = true`.

See `docs/start_host_isaac_runner.md` for the full host-runner setup.

ScenarioSpec generation uses full-scene template compatibility. A scenario
template must provide a `line_bindings` entry for every line in the released
TRT, even when a request includes a narrower `affected_lines` list.

The ENT four-line demo uses `surgical_sorting_4line_v1`. `line_1` and `line_2`
are enabled Isaac UR5 bindings. `line_3` and `line_4` are logical-only
placeholders so backend ScenarioSpec generation can proceed before matching
physical Isaac cells exist for those lines.

`surgical_sorting_v1` remains as a legacy two-line template for older fixtures.
