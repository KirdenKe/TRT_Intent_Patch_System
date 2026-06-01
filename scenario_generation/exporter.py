"""File export for generated ScenarioSpecs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scenario_generation.errors import ScenarioExportError
from scenario_generation.generator import validate_scenario_spec


def export_scenario_spec(spec: dict[str, Any], output_dir: str | Path) -> Path:
    scenario_id = spec.get("scenario_spec_id")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise ScenarioExportError("ScenarioSpec requires a scenario_spec_id before export.")
    validate_scenario_spec(spec)
    output_root = Path(output_dir)
    output_path = output_root / f"{scenario_id}.json" if output_root.name == "scenario_specs" else output_root / "scenario_specs" / f"{scenario_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return output_path
