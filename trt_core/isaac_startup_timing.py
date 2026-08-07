"""Detect the Isaac startup boundary used by Milestone 12 timing metrics."""

from __future__ import annotations

import re
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


def startup_marker_name(line: str) -> str | None:
    for name, pattern in STARTUP_MARKERS:
        if pattern.search(line):
            return name
    return None


def isaac_internal_seconds(line: str) -> float | None:
    match = _ISAAC_INTERNAL_SECONDS.search(line)
    return float(match.group("seconds")) if match else None


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
