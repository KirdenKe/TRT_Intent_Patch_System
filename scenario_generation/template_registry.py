"""Scenario template registry loading and validation."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from scenario_generation.errors import TemplateRegistryError
from scenario_generation.models import ScenarioTemplate


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "scenario_template_registry.schema.json"


def load_template_registry(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_template_registry(registry: dict[str, Any]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(registry), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        path = "/".join(str(part) for part in first.path) or "<root>"
        raise TemplateRegistryError(f"Invalid scenario template registry at {path}: {first.message}")

    template_ids = [template["template_id"] for template in registry["templates"]]
    if len(template_ids) != len(set(template_ids)):
        raise TemplateRegistryError("Scenario template registry contains duplicate template_id values.")
    if registry["default_template_id"] not in set(template_ids):
        raise TemplateRegistryError("default_template_id must reference a registered template.")


def get_template(registry: dict[str, Any], template_id: str | None = None) -> ScenarioTemplate:
    validate_template_registry(registry)
    selected_id = template_id or registry["default_template_id"]
    for template in registry["templates"]:
        if template["template_id"] == selected_id:
            return deepcopy(template)
    raise TemplateRegistryError(f"Scenario template not found: {selected_id}")

