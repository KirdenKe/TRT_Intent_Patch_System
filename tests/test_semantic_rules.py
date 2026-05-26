from __future__ import annotations

from trt_core.patch_apply import apply_intent_patch
from trt_core.semantic_rules import validate_semantics


def test_semantic_conflict_is_rejected(repo, fixture_loader):
    result = apply_intent_patch(fixture_loader("invalid_patch_semantic_conflict.json"), repo)

    assert result["status"] == "REJECTED"
    assert result["validation_results"]["semantic"] is False


def test_allowed_instruments_empty_is_semantic_error(fixture_loader):
    trt = fixture_loader("trt_v1.json")
    trt["lines"]["line_1"]["allowed_instruments"] = []

    reasons = validate_semantics(trt)

    assert any("allowed_instruments must not be empty" in reason for reason in reasons)
