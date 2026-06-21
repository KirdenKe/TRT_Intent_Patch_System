"""Semantic consistency checks for Task Requirements Tables."""

from __future__ import annotations

from typing import Any

from trt_core.models import TRT


def validate_semantics(trt: TRT | dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for line_id, line in trt.get("lines", {}).items():
        allowed = set(line.get("allowed_instruments", []))
        excluded = set(line.get("excluded_instruments", []))
        overlap = sorted(allowed & excluded)
        if overlap:
            reasons.append(f"line {line_id}: allowed_instruments and excluded_instruments overlap: {', '.join(overlap)}")
        selected_tool_ids = set(line.get("selected_tool_ids", []))
        excluded_tool_ids = set(line.get("excluded_tool_ids", []))
        tool_overlap = sorted(selected_tool_ids & excluded_tool_ids)
        if tool_overlap:
            reasons.append(f"line {line_id}: selected_tool_ids and excluded_tool_ids overlap: {', '.join(tool_overlap)}")
        priority = line.get("priority")
        if not isinstance(priority, int) or not 1 <= priority <= 5:
            reasons.append(f"line {line_id}: priority must be an integer from 1 to 5")
        manipulator_priority = line.get("manipulator_priority")
        if manipulator_priority is not None:
            policy = manipulator_priority.get("policy")
            if policy == "EXPLICIT_TOOL_ORDER" and not manipulator_priority.get("ordered_tool_ids"):
                reasons.append(f"line {line_id}: EXPLICIT_TOOL_ORDER requires ordered_tool_ids")
            if policy == "EXPLICIT_TYPE_ORDER" and not manipulator_priority.get("ordered_normalized_types"):
                reasons.append(f"line {line_id}: EXPLICIT_TYPE_ORDER requires ordered_normalized_types")

        kpi = line.get("kpi", {})
        for field in ("deadline_minutes", "max_downtime_seconds", "min_throughput_per_hour"):
            value = kpi.get(field)
            if value is not None and (not isinstance(value, int) or value < 0):
                reasons.append(f"line {line_id}: kpi.{field} must be non-negative or null")

        state = line.get("state", {})
        if line.get("abnormal_strategy") == "CONTINUE_FEASIBLE_TASKS" and state.get("mode") == "ERROR":
            reasons.append(f"line {line_id}: CONTINUE_FEASIBLE_TASKS is invalid while state.mode is ERROR")

    return reasons
