"""ScenarioSpec parsing and validation for Isaac execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _by_id(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items if isinstance(item, dict) and item.get(key)}


def _tool_catalog(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    catalog = spec.get("tool_catalog") or {}
    return catalog if isinstance(catalog, dict) else {}


def _tool_ids_for_types(catalog: dict[str, dict[str, Any]], normalized_types: list[str] | None) -> list[str]:
    if normalized_types is None:
        return []
    requested = set(normalized_types)
    return [
        tool_id
        for tool_id, tool in sorted(catalog.items())
        if isinstance(tool, dict) and tool.get("normalized_type") in requested
    ]


def _unique_tool_ids(tool_ids: list[str] | None) -> list[str]:
    return list(dict.fromkeys(tool_ids or []))


def _classify_policy_tool_ids(
    spec: dict[str, Any],
    policy: dict[str, Any],
    all_tool_ids: list[str],
) -> tuple[list[str], list[str]]:
    tool_sets = spec.get("tool_sets") or {}
    target_set_id = policy.get("target_set_id")
    if target_set_id and target_set_id in tool_sets:
        tool_set = tool_sets[target_set_id] or {}
        selected_ids = _unique_tool_ids(tool_set.get("required_tool_ids"))
        unselected_ids = _unique_tool_ids(tool_set.get("non_member_tool_ids"))
        excluded_values = _unique_tool_ids(policy.get("excluded_tool_ids"))
        excluded_lookup = set(excluded_values)
        if excluded_lookup:
            selected_ids = [tool_id for tool_id in selected_ids if tool_id not in excluded_lookup]
            unselected_ids = _unique_tool_ids([*unselected_ids, *excluded_values])
        if not unselected_ids and all_tool_ids:
            selected_lookup = set(selected_ids)
            unselected_ids = [tool_id for tool_id in all_tool_ids if tool_id not in selected_lookup]
        return selected_ids, unselected_ids

    selected = policy.get("selected_tool_ids")
    if selected is None:
        selected = _tool_ids_for_types(_tool_catalog(spec), policy.get("allowed_instruments"))
    selected_ids = _unique_tool_ids(selected)
    selected_lookup = set(selected_ids)
    return selected_ids, [tool_id for tool_id in all_tool_ids if tool_id not in selected_lookup]


def _line_ids_from_policies(spec: dict[str, Any]) -> list[str]:
    return sorted(
        policy["line_id"]
        for policy in spec.get("line_policies", [])
        if isinstance(policy, dict) and isinstance(policy.get("line_id"), str)
    )


def build_line_tooling(spec: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Build per-line wanted/unwanted tooling lists from a ScenarioSpec.

    selected_tools are wanted task items. unselected_tools are unwanted task
    items, not tools the robot is physically unable to pick up.
    """

    catalog = _tool_catalog(spec)
    all_tool_ids = sorted(catalog)
    line_tooling: dict[str, dict[str, list[str]]] = {}
    for policy in spec.get("line_policies", []):
        if not isinstance(policy, dict) or not policy.get("line_id"):
            continue
        excluded = policy.get("excluded_tool_ids")
        if excluded is None:
            excluded = _tool_ids_for_types(catalog, policy.get("excluded_instruments"))

        selected_ids, unselected_ids = _classify_policy_tool_ids(spec, policy, all_tool_ids)
        excluded_ids = _unique_tool_ids(excluded)
        line_tooling[policy["line_id"]] = {
            "selected_tools": selected_ids,
            "unselected_tools": unselected_ids,
            "excluded_tool_ids": excluded_ids,
        }
    return line_tooling


def validate_scenario_spec_for_isaac(
    spec: dict[str, Any],
    *,
    isaac_script_path: str | Path | None = None,
    output_db_path: str | Path | None = None,
) -> list[str]:
    """Return validation errors for ScenarioSpec execution by Isaac."""

    errors: list[str] = []
    for field in ("scenario_spec_id", "release_id", "trt_id", "trt_version", "reconciliation_plan_id"):
        if not spec.get(field):
            errors.append(f"ScenarioSpec is missing required field: {field}")

    policy_lines = set(_line_ids_from_policies(spec))
    bindings = _by_id(spec.get("line_bindings", []), "line_id")
    binding_lines = set(bindings)
    missing_bindings = sorted(policy_lines - binding_lines)
    if missing_bindings:
        errors.append(f"ScenarioSpec line bindings missing for lines: {missing_bindings}")

    catalog = _tool_catalog(spec)
    if not catalog:
        errors.append("ScenarioSpec is missing tool_catalog.")
    known_tool_ids = set(catalog)
    line_tooling = build_line_tooling(spec)
    for line_id, tooling in line_tooling.items():
        for group_name in ("selected_tools", "unselected_tools", "excluded_tool_ids"):
            unknown = sorted(set(tooling[group_name]) - known_tool_ids)
            if unknown:
                errors.append(f"{line_id}: {group_name} contains unknown tool IDs: {unknown}")

    if isaac_script_path is not None and not Path(isaac_script_path).exists():
        errors.append(f"Isaac Sim script path does not exist: {isaac_script_path}")

    if output_db_path is not None:
        output_parent = Path(output_db_path).parent
        if output_parent.exists() and not output_parent.is_dir():
            errors.append(f"Simulation output parent is not a directory: {output_parent}")
        elif not output_parent.exists():
            try:
                output_parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append(f"Simulation output parent is not writable: {output_parent}: {exc}")

    return errors
