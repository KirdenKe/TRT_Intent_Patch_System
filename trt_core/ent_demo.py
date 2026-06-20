"""ENT surgical tooling demo data helpers.

The demo uses instance-level tool IDs because the ENT set contains repeated
instrument types. Type-level legacy fields remain for compatibility only.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from trt_core.line_registry import load_line_registry
from trt_core.repository import TRTRepository


EXPERIMENT_ID = "ent_surgical_tooling_sorting_demo"
TRT_ID = "trt-demo"
TRT_VERSION = "v1"
STATE_VERSION = "state-demo-v1"
TARGET_SET_ID = "ENT_SURGICAL_TOOLING_SET"
SUPPORTED_TOOL_IDS = [f"tool_{index:02d}" for index in range(1, 28)]
ENT_REQUIRED_TOOL_IDS = [
    "tool_06",
    "tool_07",
    "tool_08",
    "tool_09",
    "tool_10",
    "tool_11",
    "tool_12",
    "tool_13",
    "tool_14",
    "tool_15",
    "tool_16",
    "tool_17",
    "tool_19",
    "tool_20",
    "tool_21",
    "tool_23",
    "tool_24",
    "tool_25",
    "tool_26",
    "tool_27",
]
ENT_NON_MEMBER_TOOL_IDS = ["tool_01", "tool_02", "tool_03", "tool_04", "tool_05", "tool_18", "tool_22"]
NORMALIZED_TYPES = {
    "Forceps": "FORCEPS",
    "Scissors": "SCISSORS",
    "Double-ended Surgical Retractor": "DOUBLE_ENDED_SURGICAL_RETRACTOR",
    "Surgical Forceps": "SURGICAL_FORCEPS",
    "Knife Handle": "KNIFE_HANDLE",
    "Sponge Forceps": "SPONGE_FORCEPS",
    "Needle holder": "NEEDLE_HOLDER",
    "Nerve Retractor": "NERVE_RETRACTOR",
    "Mastoid Retractor": "MASTOID_RETRACTOR",
    "Surgical Suction Cannula": "SURGICAL_SUCTION_CANNULA",
}
TOOL_TYPE_BY_NUMBER = {
    1: "Forceps",
    2: "Scissors",
    3: "Double-ended Surgical Retractor",
    4: "Surgical Forceps",
    5: "Knife Handle",
    6: "Sponge Forceps",
    7: "Needle holder",
    8: "Needle holder",
    9: "Forceps",
    10: "Forceps",
    11: "Forceps",
    12: "Sponge Forceps",
    13: "Sponge Forceps",
    14: "Forceps",
    15: "Scissors",
    16: "Knife Handle",
    17: "Knife Handle",
    18: "Knife Handle",
    19: "Surgical Forceps",
    20: "Nerve Retractor",
    21: "Double-ended Surgical Retractor",
    22: "Surgical Forceps",
    23: "Mastoid Retractor",
    24: "Surgical Suction Cannula",
    25: "Surgical Forceps",
    26: "Surgical Forceps",
    27: "Surgical Forceps",
}


def build_tool_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    ent_members = set(ENT_REQUIRED_TOOL_IDS)
    for tool_number, type_name in TOOL_TYPE_BY_NUMBER.items():
        tool_id = f"tool_{tool_number:02d}"
        catalog[tool_id] = {
            "tool_id": tool_id,
            "tool_number": tool_number,
            "type": type_name,
            "normalized_type": NORMALIZED_TYPES[type_name],
            "belongs_to_ent_set": tool_id in ent_members,
            "set_id": TARGET_SET_ID if tool_id in ent_members else None,
            "quantity_instance": 1,
        }
    return catalog


def build_line(line_id: str, binding: dict[str, Any]) -> dict[str, Any]:
    env_id = int(binding["env_id"])
    stage_root = binding["stage_robot_prim_path"].rsplit("/", 1)[0]
    return {
        "goal": "ROUTINE_CLASSIFICATION",
        "priority": 3,
        "target_set_id": TARGET_SET_ID,
        "selected_tool_ids": [],
        "excluded_tool_ids": [],
        "required_tool_ids": [],
        "allowed_instruments": [],
        "excluded_instruments": [],
        "tooling_policy": {"required_scope": "SELECTED_TOOLING"},
        "abnormal_strategy": "CONTINUE_FEASIBLE_TASKS",
        "kpi": {
            "deadline_minutes": None,
            "max_downtime_seconds": None,
            "min_throughput_per_hour": 120,
        },
        "digital_twin": {
            "robot_id": binding["robot_id"],
            "robot_model": binding["robot_model"],
            "robot_scene_name": f"ur5_robot_{env_id}",
            "workspace_id": binding["workspace_id"],
            "workspace_env_id": env_id,
            "stage_workspace_prim_path": binding.get("stage_workspace_prim_path") or stage_root,
            "stage_robot_prim_path": binding["stage_robot_prim_path"],
            "stage_end_effector_prim_path": binding.get("stage_end_effector_prim_path")
            or f"{binding['stage_robot_prim_path']}/Gripper/robotiq_arg2f_base_link",
            "stage_tooling_root_prim_path": binding.get("stage_tooling_root_prim_path") or f"{stage_root}/Tooling",
            "stage_tray_prim_path": binding.get("stage_tray_prim_path"),
            "tray_id": binding["tray_id"],
            "active_set_id": TARGET_SET_ID,
            "task_name": "ur5_pick_place",
            "line_type": binding["line_type"],
            "simulation_mode": binding["simulation_mode"],
            "physical_available": binding.get("physical_available"),
            "input_area_path": binding["input_area_path"],
            "output_area_path": binding["output_area_path"],
        },
    }


def _enabled_registry_lines(repository: TRTRepository | None = None) -> dict[str, dict[str, Any]]:
    registry = load_line_registry(repository)
    return {
        line_id: line
        for line_id, line in sorted(registry["lines"].items())
        if line.get("enabled") is True
    }


def build_trt(repository: TRTRepository | None = None) -> dict[str, Any]:
    registry_lines = _enabled_registry_lines(repository)
    return {
        "trt_id": TRT_ID,
        "version": TRT_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "tool_sets": {
            TARGET_SET_ID: {
                "set_id": TARGET_SET_ID,
                "required_tool_ids": list(ENT_REQUIRED_TOOL_IDS),
                "non_member_tool_ids": list(ENT_NON_MEMBER_TOOL_IDS),
            }
        },
        "tool_catalog": build_tool_catalog(),
        "lines": {line_id: build_line(line_id, binding) for line_id, binding in registry_lines.items()},
    }


def build_runtime_line(line_id: str, binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "RUNNING",
        "current_task": None,
        "last_exception": None,
        "wip_count": 0,
        "checkpoint": "NONE",
        "current_instruments": [],
        "active_set_id": TARGET_SET_ID,
        "selected_tool_ids": [],
        "completed_tool_ids": [],
        "pending_tool_ids": [],
        "entanglement": {
            "detected": False,
            "tool_ids": [],
            "severity": None,
            "requires_operator": False,
        },
        "locked_resources": [],
        "robot_id": binding["robot_id"],
        "workspace_id": binding["workspace_id"],
    }


def build_current_state(repository: TRTRepository | None = None) -> dict[str, Any]:
    registry_lines = _enabled_registry_lines(repository)
    return {
        "state_version": STATE_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "active_trt_id": TRT_ID,
        "active_trt_version": TRT_VERSION,
        "lines": {line_id: build_runtime_line(line_id, binding) for line_id, binding in registry_lines.items()},
    }


def state_object_to_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for line_id, line_state in sorted((state.get("lines") or {}).items()):
        record = deepcopy(line_state)
        record["line_id"] = line_id
        records.append(record)
    return records
