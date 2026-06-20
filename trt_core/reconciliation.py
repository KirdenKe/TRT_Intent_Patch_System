"""Reconciliation plan construction and persistence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from trt_core.repository import TRTRepository


def now_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_prefixed(document: Any) -> str:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def overall_status(line_decisions: list[dict[str, Any]]) -> str:
    decisions = {item["decision"] for item in line_decisions}
    if "REJECT_INCOMPATIBLE" in decisions:
        return "REJECTED"
    if "DEGRADED_SWITCH" in decisions:
        return "DEGRADED"
    if "WAIT_FOR_CHECKPOINT" in decisions:
        return "WAITING"
    return "READY"


def build_reconciliation_plan(
    *,
    trt: dict[str, Any],
    state_records: list[dict[str, Any]],
    line_decisions: list[dict[str, Any]],
    release_id: str | None = None,
    affected_lines: list[str] | None = None,
) -> dict[str, Any]:
    plan = {
        "plan_id": f"rec_{uuid4()}",
        "trt_id": trt["trt_id"],
        "trt_version": trt["version"],
        "created_at_utc": now_utc(),
        "line_decisions": line_decisions,
        "overall_status": overall_status(line_decisions),
        "source_state_hash": sha256_prefixed(state_records),
        "source_trt_hash": sha256_prefixed(trt),
    }
    if release_id is not None:
        plan["release_id"] = release_id
    if affected_lines is not None:
        plan["affected_lines"] = affected_lines
    return plan


def save_plan(plan: dict[str, Any], repository: TRTRepository | None = None) -> dict[str, Any]:
    repo = repository or TRTRepository()
    repo.save_reconciliation_plan(plan)
    return plan


def load_plan(plan_id: str, repository: TRTRepository | None = None) -> dict[str, Any]:
    repo = repository or TRTRepository()
    return repo.load_reconciliation_plan(plan_id)
