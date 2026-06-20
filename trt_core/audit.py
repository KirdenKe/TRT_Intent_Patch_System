"""Audit Bundle construction and hashing utilities."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from trt_core.models import AuditBundle, IntentPatch, TRT, ValidationResults


def canonical_json(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_document(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def build_audit_bundle(
    *,
    intent_patch: IntentPatch | dict[str, Any],
    trt_before: TRT | dict[str, Any],
    status: str,
    validation_results: ValidationResults,
    rejection_reasons: list[str],
    trt_after: TRT | dict[str, Any] | None = None,
    audit_id: str | None = None,
) -> AuditBundle:
    accepted = status == "ACCEPTED"
    return {
        "audit_id": audit_id or f"audit-{uuid4()}",
        "patch_id": intent_patch.get("patch_id", ""),
        "trt_id": intent_patch.get("trt_id", trt_before.get("trt_id", "")),
        "operator_id": intent_patch.get("operator_id", ""),
        "timestamp_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": status if status in {"ACCEPTED", "REJECTED", "REJECTED_BY_OPERATOR", "NEEDS_REVISION"} else "REJECTED",
        "trt_before_version": trt_before.get("version", ""),
        "trt_after_version": trt_after.get("version") if trt_after is not None else None,
        "trt_before_hash": sha256_document(dict(trt_before)),
        "trt_after_hash": sha256_document(dict(trt_after)) if trt_after is not None else None,
        "operations": intent_patch.get("operations", []),
        "validation_results": validation_results,
        "rejection_reasons": rejection_reasons,
        "intent_text": intent_patch.get("intent_text", ""),
        "reason": intent_patch.get("reason", ""),
        "scenario_spec_id": None,
        "run_artifact_id": None,
    }
