"""Tool classification helpers for the UR5 ScenarioSpec runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib import request


_SCENARIO_SPEC: dict[str, Any] | None = None


def set_scenario_spec_context(scenario_spec: dict[str, Any] | None) -> None:
    global _SCENARIO_SPEC
    _SCENARIO_SPEC = scenario_spec


def load_scenario_spec_context(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        set_scenario_spec_context(None)
        return None
    scenario = json.loads(Path(path).read_text(encoding="utf-8"))
    set_scenario_spec_context(scenario)
    return scenario


def _tool_number(tool_id: str) -> int:
    if isinstance(tool_id, int):
        return int(tool_id)
    text = str(tool_id)
    if text.startswith("tool_"):
        return int(text.split("_", 1)[1])
    return int(text)


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


def _line_policies(scenario_spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [policy for policy in scenario_spec.get("line_policies", []) if isinstance(policy, dict)]


def _classify_policy_tool_ids(
    scenario_spec: dict[str, Any],
    policy: dict[str, Any],
    all_tool_ids: list[str],
) -> tuple[list[str], list[str]]:
    tool_sets = scenario_spec.get("tool_sets") or {}
    target_set_id = policy.get("target_set_id")
    if target_set_id and target_set_id in tool_sets:
        tool_set = tool_sets[target_set_id] or {}
        wanted = _unique_tool_ids(tool_set.get("required_tool_ids"))
        unwanted = _unique_tool_ids(tool_set.get("non_member_tool_ids"))
        excluded_ids = _unique_tool_ids(policy.get("excluded_tool_ids"))
        excluded = set(excluded_ids)
        if excluded:
            wanted = [tool_id for tool_id in wanted if tool_id not in excluded]
            unwanted = _unique_tool_ids([*unwanted, *excluded_ids])
        if not unwanted and all_tool_ids:
            wanted_lookup = set(wanted)
            unwanted = [tool_id for tool_id in all_tool_ids if tool_id not in wanted_lookup]
        return wanted, unwanted

    selected = policy.get("selected_tool_ids")
    if selected is None:
        selected = _tool_ids_for_types(scenario_spec.get("tool_catalog") or {}, policy.get("allowed_instruments"))
    selected_ids = _unique_tool_ids(selected)
    selected_lookup = set(selected_ids)
    return selected_ids, [tool_id for tool_id in all_tool_ids if tool_id not in selected_lookup]


def build_line_tooling(scenario_spec: dict[str, Any]) -> dict[str, dict[str, list[int]]]:
    """Return wanted/unwanted tooling per line.

    All tooling instances can be picked up by the robotic arm. The classification
    is only wanted versus unwanted for the current task. Tangled tooling is an
    abnormal runtime event, not an excluded tool type.
    """

    catalog = scenario_spec.get("tool_catalog") or {}
    all_tool_ids = sorted(catalog)
    line_tooling: dict[str, dict[str, list[int]]] = {}
    for policy in _line_policies(scenario_spec):
        if not isinstance(policy, dict) or not policy.get("line_id"):
            continue
        selected_ids, unselected_ids = _classify_policy_tool_ids(scenario_spec, policy, all_tool_ids)
        line_tooling[policy["line_id"]] = {
            "selected_tools": [_tool_number(tool_id) for tool_id in selected_ids],
            "unselected_tools": [_tool_number(tool_id) for tool_id in unselected_ids],
        }
    return line_tooling


def get_tools_classification_from_scenario(
    scenario_spec: dict[str, Any],
    line_id: str | None,
) -> tuple[list[int], list[int]]:
    if not line_id:
        policies = _line_policies(scenario_spec)
        if len(policies) > 1:
            raise ValueError("line_id is required when ScenarioSpec contains multiple production lines.")
        line_id = _default_line_id(scenario_spec)
    line_tooling = build_line_tooling(scenario_spec)
    tooling = line_tooling.get(line_id, {"selected_tools": [], "unselected_tools": []})
    return list(tooling["selected_tools"]), list(tooling["unselected_tools"])


def _line_id_for_env(scenario_spec: dict[str, Any], env_id: int) -> str:
    for binding in scenario_spec.get("line_bindings", []) or []:
        if not isinstance(binding, dict):
            continue
        if int(binding.get("env_id", -1)) == int(env_id) and binding.get("line_id"):
            return str(binding["line_id"])
    raise ValueError(f"env_id {env_id} is not present in ScenarioSpec line_bindings.")


def get_tools_classification_for_env(
    scenario_spec: dict[str, Any],
    env_id: int,
) -> tuple[list[int], list[int]]:
    return get_tools_classification_from_scenario(scenario_spec, _line_id_for_env(scenario_spec, env_id))


def classify_tools_for_line(line_id: str, scenario_spec: dict[str, Any]) -> tuple[list[int], list[int]]:
    return get_tools_classification_from_scenario(scenario_spec, line_id)


def get_tools_classification(url: str | None = None, line_id: str | None = None) -> tuple[list[int], list[int]]:
    if _SCENARIO_SPEC is not None:
        return get_tools_classification_from_scenario(_SCENARIO_SPEC, line_id)
    if not url:
        return [], []
    headers = {"X-API-Key": "CompalSecureKey2025!@#"}
    http_request = request.Request(url, headers=headers)
    with request.urlopen(http_request, timeout=5.0) as response:
        data = json.loads(response.read().decode("utf-8"))
    selected_tools = list(map(stringify_tools, data.get("selected_tools", [])))
    unselected_tools = list(map(stringify_tools, data.get("unselected_tools", [])))
    return selected_tools, unselected_tools


def _default_line_id(scenario_spec: dict[str, Any]) -> str:
    policies = scenario_spec.get("line_policies") or []
    if policies and isinstance(policies[0], dict) and policies[0].get("line_id"):
        return str(policies[0]["line_id"])
    return "line_1"


def stringify_tools(tool: Any) -> int:
    if isinstance(tool, int):
        return tool
    text = str(tool)
    if "tool_" in text:
        return _tool_number(text[text.index("tool_") : text.index("tool_") + 7])
    digits = "".join(character for character in text if character.isdigit())
    return int(digits) if digits else 0
