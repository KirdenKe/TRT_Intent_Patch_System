"""Release-stage orchestration over deterministic patch validation and apply."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from trt_core.audit import build_audit_bundle
from trt_core.patch_apply import apply_intent_patch, validate_intent_patch
from trt_core.repository import TRTRepository


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def summarize_candidate(intent_patch: dict[str, Any]) -> dict[str, Any]:
    paths = [operation.get("path") for operation in intent_patch.get("operations", []) if operation.get("path")]
    affected_lines = sorted(
        {
            parts[2]
            for path in paths
            if (parts := path.split("/")) and len(parts) > 3 and parts[1] == "lines"
        }
    )
    return {
        "intent_text": intent_patch.get("intent_text"),
        "reason": intent_patch.get("reason"),
        "affected_lines": affected_lines,
        "affected_fields": paths,
    }


def prepare_release(intent_patch: dict[str, Any], repository: TRTRepository | None = None) -> dict[str, Any]:
    repo = repository or TRTRepository()
    validation = validate_intent_patch(intent_patch, repo)
    current_trt = repo.get_current_trt(intent_patch.get("trt_id"))
    timestamp = _now()
    release_record = {
        "release_id": f"rel_{uuid4()}",
        "patch_id": intent_patch.get("patch_id"),
        "trt_id": intent_patch.get("trt_id"),
        "base_version": intent_patch.get("base_version"),
        "operator_id": intent_patch.get("operator_id"),
        "candidate_patch": intent_patch,
        "candidate_summary": summarize_candidate(intent_patch),
        "validation_results_at_prepare": validation["validation_results"],
        "status": "PENDING_OPERATOR_DECISION",
        "operator_decision": None,
        "audit_id": None,
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
    }
    repo.save_release_record(release_record)
    return release_record


def record_release_decision(decision_request: dict[str, Any], repository: TRTRepository | None = None) -> dict[str, Any]:
    repo = repository or TRTRepository()
    release_record = repo.load_release_record(decision_request["release_id"])
    if release_record["status"] != "PENDING_OPERATOR_DECISION":
        raise ValueError(f"Release is not pending operator decision: {release_record['status']}")
    decision = decision_request["decision"]
    decision_timestamp = _now()
    release_record["operator_decision"] = {
        "decision": decision,
        "operator_id": decision_request.get("operator_id"),
        "comment": decision_request.get("comment"),
        "timestamp_utc": decision_timestamp,
    }
    release_record["updated_at_utc"] = decision_timestamp

    if decision == "APPROVE":
        apply_result = apply_intent_patch(release_record["candidate_patch"], repo, release_id=release_record["release_id"])
        if apply_result["status"] == "ACCEPTED":
            release_record["status"] = "RELEASED"
        else:
            release_record["status"] = (
                "FAILED_STALE_VERSION" if not apply_result["validation_results"]["base_version"] else "NEEDS_REVISION"
            )
        release_record["audit_id"] = apply_result["audit_id"]
        release_record["trt_version"] = apply_result.get("trt_version")
        release_record["previous_trt_version"] = apply_result.get("previous_trt_version")
        release_record["version_path"] = apply_result.get("version_path")
        release_record["current_trt_path"] = apply_result.get("current_trt_path")
        repo.save_release_record(release_record)
        return release_record

    if decision == "REJECT":
        audit_bundle = _decision_audit_bundle(
            repo=repo,
            release_record=release_record,
            status="REJECTED_BY_OPERATOR",
            rejection_reasons=[decision_request.get("comment") or "Operator rejected release."],
        )
        repo.save_audit_bundle(audit_bundle)
        release_record["status"] = "REJECTED_BY_OPERATOR"
        release_record["audit_id"] = audit_bundle["audit_id"]
        repo.save_release_record(release_record)
        return release_record

    if decision == "REQUEST_REVISION":
        audit_bundle = _decision_audit_bundle(
            repo=repo,
            release_record=release_record,
            status="NEEDS_REVISION",
            rejection_reasons=[decision_request.get("comment") or "Operator requested revision."],
        )
        repo.save_audit_bundle(audit_bundle)
        release_record["status"] = "NEEDS_REVISION"
        release_record["audit_id"] = audit_bundle["audit_id"]
        repo.save_release_record(release_record)
        return release_record

    raise ValueError(f"Unsupported release decision: {decision}")


def _decision_audit_bundle(
    *,
    repo: TRTRepository,
    release_record: dict[str, Any],
    status: str,
    rejection_reasons: list[str],
) -> dict[str, Any]:
    current_trt = repo.get_current_trt(release_record["candidate_patch"].get("trt_id"))
    validation = validate_intent_patch(release_record["candidate_patch"], repo)
    audit_patch = dict(release_record["candidate_patch"])
    operator_decision = release_record.get("operator_decision") or {}
    audit_patch["operator_id"] = operator_decision.get("operator_id") or audit_patch.get("operator_id")
    return build_audit_bundle(
        intent_patch=audit_patch,
        trt_before=current_trt,
        status=status,
        validation_results=validation["validation_results"],
        rejection_reasons=rejection_reasons,
        trt_after=None,
    )
