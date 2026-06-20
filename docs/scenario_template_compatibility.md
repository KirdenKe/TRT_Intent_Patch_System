# Scenario Template Compatibility

ScenarioSpec generation uses full-scene template compatibility. A scenario
template must provide a `line_bindings` entry for every line in the released
TRT, even when a request includes a narrower `affected_lines` list.

The ENT four-line demo uses `surgical_sorting_4line_v1`. `line_1` and `line_2`
are enabled Isaac UR5 bindings. `line_3` and `line_4` are logical-only
placeholders so backend ScenarioSpec generation can proceed before matching
physical Isaac cells exist for those lines.

`surgical_sorting_v1` remains as a legacy two-line template for older fixtures.
