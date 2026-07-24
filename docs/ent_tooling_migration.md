# ENT Tooling Data Migration

## Fresh Clone Setup

From a new clone, set up the Python package and tests before editing generated ENT data:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest
```

Build and start the application containers:

```powershell
docker compose build trt-api
docker compose up -d trt-api
Invoke-RestMethod http://localhost:8000/health
```

For Isaac-backed runs, configure `host_runner`:

1. Update `data/isaac_host_config.json` with the Windows host project root and Isaac Sim paths for this clone.
2. Add `.env` with `ISAAC_HOST_RUNNER_URL=http://host.docker.internal:8765`.
3. Start `.\scripts\start_host_isaac_runner.ps1 -Port 8765` from Windows PowerShell.
4. Recreate the API container with `docker compose up -d --force-recreate trt-api`.
5. Check `Invoke-RestMethod http://localhost:8000/debug/isaac-host-runner-status`.

Use `docs/start_host_isaac_runner.md` for detailed troubleshooting.

The original demo used `allowed_instruments` and `excluded_instruments` as
type-level strategy fields. That is no longer precise enough for the ENT
surgical tooling experiment because the set contains repeated instrument types.
For example, `tool_07` and `tool_08` are distinct physical Needle holders.

The new source of truth is instance-level tooling:

- `selected_tool_ids`: physical tool instances selected for the current strategy.
- `excluded_tool_ids`: physical tool instances explicitly excluded by operator policy.
- `required_tool_ids`: physical tool instances required by a tool set or strategy.
- `tool_catalog`: the full catalog of 27 physical tool instances.
- `tool_sets`: named set membership, including `ENT_SURGICAL_TOOLING_SET`.

Legacy fields remain only for compatibility:

- `allowed_instruments` is a derived type-level view of selected tooling.
- `excluded_instruments` is a derived type-level view of explicit exclusions.
- Neither field represents robot physical capability.

Entanglement is runtime state, not a tooling exclusion. It belongs in
`data/state_records/current_state.json` under each line's `entanglement` object
and must not remove tools from `selected_tool_ids` or `excluded_tool_ids`.
