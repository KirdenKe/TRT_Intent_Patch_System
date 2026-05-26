"""Patch validation and application orchestration."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import jsonpatch

from trt_core.audit import build_audit_bundle
from trt_core.models import IntentPatch
from trt_core.repository import TRTRepository
from trt_core.semantic_rules import validate_semantics
from trt_core.validator import default_validation_results, validate_firewall, validate_trt_schema


def validate_intent_patch(intent_patch: IntentPatch | dict[str, Any], repository: TRTRepository | None = None) -> dict[str, Any]:
    repo = repository or TRTRepository()
    current_trt = repo.get_current_trt(intent_patch.get("trt_id"))
    validation_results, rejection_reasons = validate_firewall(intent_patch, current_trt)
    if all(validation_results.values()):
        try:
            patched = jsonpatch.apply_patch(deepcopy(current_trt), intent_patch.get("operations", []), in_place=False)
            patched["version"] = repo.next_version(current_trt["version"])
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


def apply_intent_patch(intent_patch: IntentPatch | dict[str, Any], repository: TRTRepository | None = None) -> dict[str, Any]:
    repo = repository or TRTRepository()
    current_trt = repo.get_current_trt(intent_patch.get("trt_id"))
    validation_results, rejection_reasons = validate_firewall(intent_patch, current_trt)
    patched_trt: dict[str, Any] | None = None

    if all(validation_results.values()):
        try:
            patched_trt = jsonpatch.apply_patch(deepcopy(current_trt), intent_patch.get("operations", []), in_place=False)
            patched_trt["version"] = repo.next_version(current_trt["version"])
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
    if accepted:
        repo.save_trt(patched_trt)

    audit_bundle = build_audit_bundle(
        intent_patch=intent_patch,
        trt_before=current_trt,
        trt_after=patched_trt if accepted else None,
        status="ACCEPTED" if accepted else "REJECTED",
        validation_results=validation_results,
        rejection_reasons=rejection_reasons,
    )
    repo.save_audit_bundle(audit_bundle)

    return {
        "status": audit_bundle["status"],
        "audit_id": audit_bundle["audit_id"],
        "trt_version": audit_bundle.get("trt_after_version") or audit_bundle["trt_before_version"],
        "validation_results": validation_results,
        "rejection_reasons": rejection_reasons,
        "audit_bundle": audit_bundle,
    }

