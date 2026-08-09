"""Detect the Isaac startup boundary used by Milestone 12 timing metrics."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable


_ARTICULATION_PATH = r"/World/Envs/Env\d+/ur5/Gripper/robotiq_arg2f_base_link"
STARTUP_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "NON_ROOT_ARTICULATION_TRANSFORM",
        re.compile(rf"Cannot assign transform to non-root articulation link.*{_ARTICULATION_PATH}"),
    ),
    (
        "RIGID_BODY_VELOCITY",
        re.compile(rf"Cannot assign velocities to rigid body.*{_ARTICULATION_PATH}"),
    ),
    (
        "GPU_MEMORY_BUDGET_FACTORY_WARNING",
        re.compile(
            r"Client gpu\.foundation\.plugin has acquired "
            r"\[gpu::unstable::IMemoryBudgetManagerFactory v0\.1\] 100 times"
        ),
    ),
)
_ISAAC_INTERNAL_SECONDS = re.compile(r"\[\s*(?P<seconds>\d+(?:\.\d+)?)s\]")
_LOG_UTC_TIMESTAMP = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)"
)
_INITIAL_MARKER_BURST_GAP_SECONDS = 5.0


def startup_marker_name(line: str) -> str | None:
    for name, pattern in STARTUP_MARKERS:
        if pattern.search(line):
            return name
    return None


def isaac_internal_seconds(line: str) -> float | None:
    match = _ISAAC_INTERNAL_SECONDS.search(line)
    return float(match.group("seconds")) if match else None


def _log_utc_timestamp(line: str) -> str | None:
    match = _LOG_UTC_TIMESTAMP.search(line)
    return match.group("timestamp") if match else None


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def finalized_startup_timing(
    lines: Iterable[str],
    *,
    command_started_at_utc: str | None = None,
) -> dict[str, Any] | None:
    """Select the terminal marker from the initial Isaac startup sequence.

    Isaac can emit the articulation warnings again while shutting down. Those
    repetitions are not startup evidence. The first GPU memory-budget warning
    terminates the initial sequence when present; otherwise the end of the
    first contiguous articulation-warning burst is used.
    """

    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        marker = startup_marker_name(line)
        if marker is None:
            continue
        events.append(
            {
                "line_number": line_number,
                "pattern": marker,
                "log_timestamp_utc": _log_utc_timestamp(line),
                "isaac_internal_seconds": isaac_internal_seconds(line),
            }
        )
    if not events:
        return None

    gpu_event = next(
        (event for event in events if event["pattern"] == "GPU_MEMORY_BUDGET_FACTORY_WARNING"),
        None,
    )
    if gpu_event is not None:
        selected = gpu_event
    else:
        selected = events[0]
        previous_seconds = selected["isaac_internal_seconds"]
        for event in events[1:]:
            current_seconds = event["isaac_internal_seconds"]
            if (
                previous_seconds is not None
                and current_seconds is not None
                and current_seconds - previous_seconds > _INITIAL_MARKER_BURST_GAP_SECONDS
            ):
                break
            selected = event
            previous_seconds = current_seconds

    log_timestamp = selected["log_timestamp_utc"]
    if log_timestamp and command_started_at_utc:
        startup_seconds = max(
            0.0,
            (_parse_utc(log_timestamp) - _parse_utc(command_started_at_utc)).total_seconds(),
        )
        source = "ISAAC_LOG_UTC_TIMESTAMP"
        reference_at = log_timestamp
    elif selected["isaac_internal_seconds"] is not None:
        startup_seconds = float(selected["isaac_internal_seconds"])
        source = "ISAAC_INTERNAL_TIMESTAMP"
        reference_at = None
    else:
        return None

    return {
        "startup_reference_at_utc": reference_at,
        "startup_reference_source": source,
        "startup_reference_pattern": selected["pattern"],
        "startup_reference_line_number": selected["line_number"],
        "isaac_startup_seconds": startup_seconds,
        "startup_marker_count": len(events),
        "data_quality_status": "OK" if source == "ISAAC_LOG_UTC_TIMESTAMP" else "FALLBACK_INTERNAL_TIMESTAMP",
    }


def fallback_startup_timing(lines: Iterable[str]) -> dict[str, Any] | None:
    """Use the latest Isaac internal timestamp when wall-clock capture is unavailable."""

    latest: dict[str, Any] | None = None
    marker_count = 0
    for line_number, line in enumerate(lines, start=1):
        marker = startup_marker_name(line)
        if marker is None:
            continue
        marker_count += 1
        internal_seconds = isaac_internal_seconds(line)
        if internal_seconds is None:
            continue
        if latest is None or internal_seconds >= latest["isaac_startup_seconds"]:
            latest = {
                "startup_reference_source": "ISAAC_INTERNAL_TIMESTAMP",
                "startup_reference_pattern": marker,
                "startup_reference_line_number": line_number,
                "isaac_startup_seconds": internal_seconds,
            }
    if latest is not None:
        latest["startup_marker_count"] = marker_count
        latest["data_quality_status"] = "FALLBACK_INTERNAL_TIMESTAMP"
    return latest
