"""Typed helpers for ScenarioSpec generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict
from uuid import uuid4


class ScenarioTemplate(TypedDict):
    template_id: str
    scene_template: str
    workspace_contract: dict[str, Any]
    simulation_config: dict[str, Any]
    line_bindings: list[dict[str, Any]]
    operator_model: dict[str, Any]
    abnormal_event_policy: dict[str, Any]
    assertions: dict[str, Any]


class ScenarioSpec(TypedDict):
    scenario_spec_id: str
    release_id: str
    trt_id: str
    trt_version: str
    reconciliation_plan_id: str
    candidate_strategy_id: str
    workspace_contract: dict[str, Any]
    scene_template: str
    simulation_config: dict[str, Any]
    line_bindings: list[dict[str, Any]]
    line_policies: list[dict[str, Any]]
    operator_model: dict[str, Any]
    abnormal_event_policy: dict[str, Any]
    assertions: dict[str, Any]


class WaitingForCheckpointResult(TypedDict):
    status: str
    release_id: str
    trt_id: str
    trt_version: str
    reconciliation_plan_id: str
    candidate_strategy_id: str
    required_checkpoints: list[dict[str, Any]]


@dataclass(frozen=True)
class ScenarioGenerationRequest:
    released_trt: dict[str, Any]
    state_records: list[dict[str, Any]]
    reconciliation_plan: dict[str, Any]
    template_registry: dict[str, Any]
    template_id: str | None = None
    release_id: str | None = None
    candidate_strategy_id: str | None = None
    include_waiting_scenarios: bool = False


def now_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def new_scenario_spec_id() -> str:
    return f"scn_{uuid4()}"
