from __future__ import annotations

from trt_core.audit import sha256_document
from trt_core.patch_apply import apply_intent_patch
from trt_core.validator import validate_audit_bundle_schema


def test_failed_patch_creates_audit_bundle(repo, valid_patch):
    valid_patch["operations"] = [
        {
            "op": "replace",
            "path": "/lines/line_1/state/mode",
            "value": "PAUSED"
        }
    ]

    result = apply_intent_patch(valid_patch, repo)
    audit = repo.load_audit_bundle(result["audit_id"])

    assert audit["status"] == "REJECTED"
    assert audit["trt_after_version"] is None
    assert audit["trt_after_hash"] is None
    assert validate_audit_bundle_schema(audit) == []


def test_accepted_patch_creates_before_and_after_hashes(repo, valid_patch):
    before = repo.get_current_trt("trt-demo")

    result = apply_intent_patch(valid_patch, repo)
    audit = repo.load_audit_bundle(result["audit_id"])
    after = repo.get_current_trt("trt-demo")

    assert audit["status"] == "ACCEPTED"
    assert audit["trt_before_hash"] == sha256_document(before)
    assert audit["trt_after_hash"] == sha256_document(after)
    assert audit["trt_before_hash"] != audit["trt_after_hash"]
    assert validate_audit_bundle_schema(audit) == []
