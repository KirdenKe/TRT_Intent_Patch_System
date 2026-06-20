# Data-Driven Production Lines

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
