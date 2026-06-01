from __future__ import annotations

import json

import pytest

from scenario_generation.errors import ScenarioExportError
from scenario_generation.exporter import export_scenario_spec
from scenario_generation.generator import generate_scenario_spec
from scenario_generation.models import ScenarioGenerationRequest


def make_spec(fixture_loader):
    request = ScenarioGenerationRequest(
        released_trt=fixture_loader("released_trt_v1.json"),
        state_records=fixture_loader("state_records_v1.json"),
        reconciliation_plan=fixture_loader("reconciliation_ready.json"),
        template_registry=fixture_loader("scenario_templates.json"),
        release_id="rel_export_001",
        candidate_strategy_id="strategy_export_001",
    )
    return generate_scenario_spec(request)


def test_export_scenario_spec_writes_json_file(tmp_path, fixture_loader):
    spec = make_spec(fixture_loader)

    output_path = export_scenario_spec(spec, tmp_path / "outputs")
    loaded = json.loads(output_path.read_text(encoding="utf-8"))

    assert output_path.name == f"{spec['scenario_spec_id']}.json"
    assert output_path.parent == tmp_path / "outputs" / "scenario_specs"
    assert loaded == spec
    assert loaded["workspace_contract"]["exchange_mode"] == "file"


def test_export_rejects_invalid_scenario_spec(tmp_path, fixture_loader):
    spec = make_spec(fixture_loader)
    spec.pop("scenario_spec_id")

    with pytest.raises(ScenarioExportError):
        export_scenario_spec(spec, tmp_path)
