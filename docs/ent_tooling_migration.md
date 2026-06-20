# ENT Tooling Data Migration

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

