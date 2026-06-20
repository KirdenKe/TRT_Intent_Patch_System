"""Patch validation and application orchestration."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import logging
from typing import Any
from uuid import uuid4

import jsonpatch

from trt_core.audit import build_audit_bundle
from trt_core.models import IntentPatch
from trt_core.repository import TRTRepository
from trt_core.semantic_rules import validate_semantics
from trt_core.validator import default_validation_results, migrate_legacy_tooling_policy, validate_firewall, validate_trt_schema


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _line_tooling_policies(trt: dict[str, Any]) -> dict[str, Any]:
    return {
        line_id: line.get("tooling_policy")
        for line_id, line in trt.get("lines", {}).items()
        if "tooling_policy" in line
    }


def validate_intent_patch(intent_patch: IntentPatch | dict[str, Any], repository: TRTRepository | None = None) -> dict[str, Any]:
    repo = repository or TRTRepository()
    raw_current_trt = repo.get_current_trt(intent_patch.get("trt_id"))
    logger.info("patch_validate.current_trt.tooling_policy.raw=%r", _line_tooling_policies(raw_current_trt))
    current_trt = migrate_legacy_tooling_policy(raw_current_trt)
    logger.info("patch_validate.current_trt.tooling_policy.migrated=%r", _line_tooling_policies(current_trt))
    validation_results, rejection_reasons = validate_firewall(intent_patch, current_trt)
    if all(validation_results.values()):
        try:
            patched = jsonpatch.apply_patch(deepcopy(current_trt), intent_patch.get("operations", []), in_place=False)
            patched = migrate_legacy_tooling_policy(patched)
            patched["version"] = repo.next_released_version(current_trt["trt_id"])
            schema_reasons = validate_trt_schema(patched)
            semantic_reasons = validate_semantics(patched) if not schema_reasons else []
            if schema_reasons:
                validation_results["schema"] = False
                rejection_reasons.extend(f"schema: {reason}" for reason in schema_reasons)
            if semantic_reasons:
                validation_results["semantic"] = False
                rejection_reasons.extend(f"semantic: {reason}" for reason in semantic_reasons)
        except Exception as exc:
            validation_results["schema"] = False
            rejection_reasons.append(f"patch application failed: {exc}")

    status = "ACCEPTED" if all(validation_results.values()) else "REJECTED"
    return {
        "status": status,
        "validation_results": validation_results,
        "rejection_reasons": rejection_reasons,
    }


def apply_intent_patch(
    intent_patch: IntentPatch | dict[str, Any],
    repository: TRTRepository | None = None,
    *,
    release_id: str | None = None,
) -> dict[str, Any]:
    repo = repository or TRTRepository()
    current_trt = migrate_legacy_tooling_policy(repo.get_current_trt(intent_patch.get("trt_id")))
    validation_results, rejection_reasons = validate_firewall(intent_patch, current_trt)
    patched_trt: dict[str, Any] | None = None
    save_result: dict[str, Any] | None = None

    if all(validation_results.values()):
        try:
            patched_trt = jsonpatch.apply_patch(deepcopy(current_trt), intent_patch.get("operations", []), in_place=False)
            patched_trt = migrate_legacy_tooling_policy(patched_trt)
            next_version = repo.next_released_version(current_trt["trt_id"])
            patched_trt["trt_id"] = current_trt["trt_id"]
            patched_trt["version"] = next_version
            patched_trt["previous_version"] = current_trt["version"]
            patched_trt["released_at"] = _now()
            if release_id:
                patched_trt["release_id"] = release_id
            schema_reasons = validate_trt_schema(patched_trt)
            semantic_reasons = validate_semantics(patched_trt) if not schema_reasons else []
            if schema_reasons:
                validation_results["schema"] = False
                rejection_reasons.extend(f"schema: {reason}" for reason in schema_reasons)
                patched_trt = None
            if semantic_reasons:
                validation_results["semantic"] = False
                rejection_reasons.extend(f"semantic: {reason}" for reason in semantic_reasons)
                patched_trt = None
        except Exception as exc:
            validation_results["schema"] = False
            rejection_reasons.append(f"patch application failed: {exc}")
            patched_trt = None

    accepted = all(validation_results.values()) and patched_trt is not None
    audit_id = f"audit-{uuid4()}"
    if accepted:
        patched_trt["audit_id"] = audit_id

    audit_bundle = build_audit_bundle(
        intent_patch=intent_patch,
        trt_before=current_trt,
        trt_after=patched_trt if accepted else None,
        status="ACCEPTED" if accepted else "REJECTED",
        validation_results=validation_results,
        rejection_reasons=rejection_reasons,
        audit_id=audit_id,
    )
    if accepted:
        save_result = repo.save_released_trt_version(patched_trt)
    repo.save_audit_bundle(audit_bundle)

    return {
        "status": audit_bundle["status"],
        "audit_id": audit_bundle["audit_id"],
        "trt_version": audit_bundle.get("trt_after_version") or audit_bundle["trt_before_version"],
        "previous_trt_version": audit_bundle["trt_before_version"],
        "version_path": (save_result or {}).get("version_path"),
        "current_trt_path": (save_result or {}).get("current_trt_path"),
        "validation_results": validation_results,
        "rejection_reasons": rejection_reasons,
        "audit_bundle": audit_bundle,
    }
