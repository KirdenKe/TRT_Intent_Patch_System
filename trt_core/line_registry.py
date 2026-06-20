"""Production-line topology registry helpers."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from trt_core.errors import RepositoryError
from trt_core.repository import PROJECT_ROOT, TRTRepository


VALID_SIMULATION_MODES = {"PHYSICAL_OR_DIGITAL_TWIN", "LOGICAL_ONLY"}


def _registry_path(repository: TRTRepository | None = None):
    repo = repository or TRTRepository()
    path = repo.root / "data" / "production_lines" / "line_registry.json"
    if path.exists():
        return path
    return PROJECT_ROOT / "data" / "production_lines" / "line_registry.json"


def load_line_registry(repository: TRTRepository | None = None) -> dict[str, Any]:
    path = _registry_path(repository)
    if not path.exists():
        raise RepositoryError(f"Line registry not found: {path}")
    registry = json.loads(path.read_text(encoding="utf-8"))
    reasons = validate_line_registry(registry)
    if reasons:
        raise RepositoryError("; ".join(reasons))
    return registry


def validate_line_registry(registry: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not isinstance(registry.get("registry_id"), str) or not registry.get("registry_id"):
        reasons.append("registry_id is required")
    if not isinstance(registry.get("experiment_id"), str) or not registry.get("experiment_id"):
        reasons.append("experiment_id is required")
    if not isinstance(registry.get("default_scenario_template_id"), str) or not registry.get("default_scenario_template_id"):
        reasons.append("default_scenario_template_id is required")
    lines = registry.get("lines")
    if not isinstance(lines, dict) or not lines:
        reasons.append("lines must be a non-empty object")
        return reasons
    required = {
        "enabled",
        "line_type",
        "env_id",
        "robot_id",
        "robot_model",
        "workspace_id",
        "tray_id",
        "stage_robot_prim_path",
        "stage_tray_prim_path",
        "input_area_path",
        "output_area_path",
        "simulation_mode",
    }
    for line_id, line in sorted(lines.items()):
        if not isinstance(line_id, str) or not line_id:
            reasons.append("line ids must be non-empty strings")
        if not isinstance(line, dict):
            reasons.append(f"{line_id}: line entry must be an object")
            continue
        missing = sorted(field for field in required if field not in line)
        if missing:
            reasons.append(f"{line_id}: missing registry fields: {', '.join(missing)}")
        if not isinstance(line.get("enabled"), bool):
            reasons.append(f"{line_id}: enabled must be boolean")
        if line.get("simulation_mode") not in VALID_SIMULATION_MODES:
            reasons.append(f"{line_id}: unsupported simulation_mode {line.get('simulation_mode')!r}")
        if "env_id" in line and (not isinstance(line["env_id"], int) or line["env_id"] < 0):
            reasons.append(f"{line_id}: env_id must be a non-negative integer")
        for field in required - {"enabled", "env_id"}:
            if field in line and line[field] is not None and not isinstance(line[field], str):
                reasons.append(f"{line_id}: {field} must be a string or null")
    return reasons


def get_enabled_line_ids(repository: TRTRepository | None = None) -> list[str]:
    registry = load_line_registry(repository)
    return sorted(line_id for line_id, line in registry["lines"].items() if line.get("enabled") is True)


def get_line_binding(repository: TRTRepository | None, line_id: str) -> dict[str, Any]:
    registry = load_line_registry(repository)
    lines = registry["lines"]
    if line_id not in lines:
        raise RepositoryError(f"Line registry entry not found: {line_id}")
    line = deepcopy(lines[line_id])
    line["line_id"] = line_id
    return line


def resolve_line_bindings(repository: TRTRepository | None, line_ids: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    registry = load_line_registry(repository)
    resolved: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for line_id in line_ids:
        line = registry["lines"].get(line_id)
        if line is None:
            missing.append(line_id)
        else:
            binding = deepcopy(line)
            binding["line_id"] = line_id
            resolved[line_id] = binding
    return resolved, missing
