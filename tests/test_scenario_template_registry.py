from __future__ import annotations

from copy import deepcopy

import pytest

from scenario_generation.errors import TemplateRegistryError
from scenario_generation.template_registry import get_template, validate_template_registry


def test_valid_template_registry_loads_default_template(fixture_loader):
    registry = fixture_loader("scenario_templates.json")

    template = get_template(registry)

    assert template["template_id"] == "ur5_pick_place_minimal"
    assert template["workspace_contract"]["exchange_mode"] == "file"
    assert template["line_bindings"][0] == {"line_id": "line_1", "env_id": 2}


def test_registry_rejects_missing_default_template(fixture_loader):
    registry = deepcopy(fixture_loader("scenario_templates.json"))
    registry["default_template_id"] = "missing_template"

    with pytest.raises(TemplateRegistryError, match="default_template_id"):
        validate_template_registry(registry)


def test_registry_rejects_manual_event_injection_template(fixture_loader):
    registry = deepcopy(fixture_loader("scenario_templates.json"))
    registry["templates"][0]["abnormal_event_policy"]["entanglement"]["generation_mode"] = "manual_event_injection"

    with pytest.raises(TemplateRegistryError, match="generation_mode"):
        validate_template_registry(registry)
