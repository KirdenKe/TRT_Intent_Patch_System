# pick_up_example.py Argument Mapping

`pick_up_example.py` is the Isaac Sim UR5 entry point. The Dockerized TRT API does not execute it directly; `/simulation/run` builds a host-runner request and the Windows host runner expands that request into the CLI command.

The host runner passes the original command-compatible Isaac arguments plus the two governed result-output arguments now supported by the entry point: `--run_id` and `--output_db_path`. `--seed_db_path` remains the layout/seed input database and must not be used as the per-run KPI/result database.

| CLI arg | Source in ScenarioSpec/config | Current default | Host-runner behavior | Notes |
| ------- | ----------------------------- | --------------: | -------------------- | ----- |
| `--num_envs` | `scenario_spec.simulation_config.num_envs` | Enabled line binding count | Always passed | Current expected value is `4`. |
| `--headless` | `scenario_spec.simulation_config.headless` | `false` | Passed as `--headless true` or `--headless false` | Rendering remains enabled by default unless an approved override sets headless mode. |
| `--global_seed` | `scenario_spec.simulation_config.global_seed` | `null` | Omitted when null; passed only when explicitly approved | If present, `--reuse_verified_seed` is omitted. |
| `--max_seed_trials` | Internal developer mode only | omitted | Omitted by default | Restricted sweep parameter; normal operator requests must not set it. |
| `--seed_db_path` | host-runner config only | host config or script default | Passed only when non-empty and the host-visible path exists | Infrastructure path. It is not operator-controlled ScenarioSpec policy. |
| `--run_id` | Host runner generated run ID from `/simulation/run` | `sim_<uuid>` | Always passed by host runner | Identifies rows in the per-run result SQLite database. |
| `--output_db_path` | Host-visible mapped `outputs/run_artifacts/sim_<run_id>.sqlite` | Host runner request output path | Always passed by host runner | Per-run KPI/result database. This is different from `seed_sweep.sqlite3`. |
| `--reuse_verified_seed` | Governed default from ScenarioSpec policy | `true` when `global_seed` is absent | Boolean flag; passed only when true | Removed when an explicit `global_seed` is present. |
| `--reuse_precomputed_layouts` | Internal developer mode only | omitted | Omitted by default | Restricted layout-cache behavior; normal operator requests must not set it. |
| `--layout_source` | `scenario_spec.simulation_config.layout_source` from template/internal config | `auto` | Always passed | Valid values are `online`, `database`, and `auto`. Normal operator requests cannot change it. |
| `--episode_success_requires_reset_cycles` | `scenario_spec.simulation_config.episode_success_requires_reset_cycles` | `1` | Always passed | Matches current automation contract. |
| `--allowed_overlap_ratio` | `scenario_spec.simulation_config.allowed_overlap_ratio` | `0.99` | Always passed | Do not confuse this with `abnormal_event_policy.entanglement.allowed_overlap_ratio`; they are separate semantics. |
| `--chosen_intervention_mode` | `scenario_spec.simulation_config.chosen_intervention_mode`, derived from line abnormal strategy if absent | `continue-until-arrival` | Always passed | `CONTINUE_FEASIBLE_TASKS` maps to `continue-until-arrival`; explicit stop behavior maps to `immediate-stop`. |
| `--travel_time` | `scenario_spec.operator_model.travel_time` | `5.0` | Always passed | Host runner uses the ScenarioSpec operator model, not the script internal default. |
| `--fix_duration` | `scenario_spec.operator_model.fix_duration` | `8.0` | Always passed | Host runner uses the ScenarioSpec operator model, not the script internal default. |
| `--resume_delay` | `scenario_spec.operator_model.resume_delay` | `0.5` | Always passed | Host runner uses the ScenarioSpec operator model. |
| `--add_reference_number` | `scenario_spec.simulation_config.add_reference_number` | Tool catalog count | Always passed | Current expected value is `27`. |

Known working command core:

```powershell
C:\Dev\IsaacSim\_build\windows-x86_64\release\python.bat `
  C:\Dev\IsaacSim\_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5\pick_up_example.py `
  --num_envs 4 `
  --headless false `
  --allowed_overlap_ratio 0.99 `
  --layout_source auto `
  --episode_success_requires_reset_cycles 1 `
  --chosen_intervention_mode continue-until-arrival `
  --travel_time 5.0 `
  --fix_duration 8.0 `
  --resume_delay 0.5 `
  --add_reference_number 27 `
  --run_id sim_<id> `
  --output_db_path C:\Users\$93I000-7RFCRA0J9IC9\Documents\Docker\n8n_data\trt_intent_patch_system\outputs\run_artifacts\sim_<id>.sqlite `
  --seed_db_path C:\Dev\IsaacSim\_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5\tasks\seed_sweep.sqlite3 `
  --reuse_verified_seed
```

The dry-run endpoint `POST /isaac/dry-run` shows the exact expanded host command without launching Isaac Sim.
