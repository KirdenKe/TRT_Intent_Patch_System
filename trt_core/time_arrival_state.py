"""Persisted Time-Arrival Model state used by prompts and ScenarioSpecs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trt_core.repository import TRTRepository


TIME_ARRIVAL_FIELDS = ("travel_time", "fix_duration", "resume_delay")
DEFAULT_TIME_ARRIVAL = {
    "travel_time": 1.0,
    "fix_duration": 3.0,
    "resume_delay": 1.0,
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _state_path(repository: TRTRepository) -> Path:
    return repository.state_dir / "time_arrival_model.json"


def _deployed_defaults_path(repository: TRTRepository) -> Path:
    return repository.root / "data" / "digital_twin" / "default_simulation_config.json"


def _validated_values(values: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for field in TIME_ARRIVAL_FIELDS:
        value = values.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be a number.")
        number = float(value)
        if number < 0:
            raise ValueError(f"{field} must be greater than or equal to zero.")
        result[field] = number
    return result


def load_time_arrival_state(repository: TRTRepository | None = None) -> dict[str, Any]:
    """Load the authoritative state record, initializing it from deployed defaults."""

    repo = repository or TRTRepository()
    path = _state_path(repo)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        _validated_values(payload)
        return payload

    source = "BUILT_IN_DEFAULT"
    values = dict(DEFAULT_TIME_ARRIVAL)
    defaults_path = _deployed_defaults_path(repo)
    if defaults_path.exists():
        try:
            deployed = json.loads(defaults_path.read_text(encoding="utf-8"))
            config = deployed.get("simulation_config") or {}
            if all(config.get(field) is not None for field in TIME_ARRIVAL_FIELDS):
                values = _validated_values(config)
                source = "DEPLOYED_SIMULATION_DEFAULTS"
        except (json.JSONDecodeError, ValueError):
            source = "BUILT_IN_DEFAULT_AFTER_INVALID_DEPLOYED_DEFAULTS"

    return save_time_arrival_state(
        values,
        repository=repo,
        source=source,
        source_reference=str(defaults_path.relative_to(repo.root)) if defaults_path.exists() else None,
    )


def save_time_arrival_state(
    values: dict[str, Any],
    *,
    repository: TRTRepository | None = None,
    source: str,
    source_reference: str | None = None,
    run_id: str | None = None,
    scenario_spec_id: str | None = None,
) -> dict[str, Any]:
    repo = repository or TRTRepository()
    normalized = _validated_values(values)
    previous_version = 0
    path = _state_path(repo)
    if path.exists():
        try:
            previous_version = int(json.loads(path.read_text(encoding="utf-8")).get("state_version") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            previous_version = 0
    record = {
        **normalized,
        "state_record_type": "TIME_ARRIVAL_MODEL",
        "state_version": previous_version + 1,
        "source": source,
        "source_reference": source_reference,
        "run_id": run_id,
        "scenario_spec_id": scenario_spec_id,
        "updated_at_utc": _now_utc(),
    }
    repo._atomic_write_json(path, record, overwrite=True)
    return record


def time_arrival_prompt_context(repository: TRTRepository | None = None) -> dict[str, Any]:
    state = load_time_arrival_state(repository)
    return {
        field: state[field]
        for field in TIME_ARRIVAL_FIELDS
    } | {
        "state_version": state["state_version"],
        "updated_at_utc": state["updated_at_utc"],
        "source": state["source"],
    }
