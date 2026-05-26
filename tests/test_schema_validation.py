from __future__ import annotations

from trt_core.patch_apply import apply_intent_patch
from trt_core.validator import validate_intent_patch_schema, validate_trt_schema


def test_trt_and_intent_patch_fixtures_match_schema(fixture_loader):
    assert validate_trt_schema(fixture_loader("trt_v1.json")) == []
    assert validate_intent_patch_schema(fixture_loader("valid_patch.json")) == []


def test_invalid_field_is_rejected(repo, valid_patch):
    valid_patch["operations"] = [
        {
            "op": "replace",
            "path": "/lines/line_1/unapproved_field",
            "value": "anything"
        }
    ]

    result = apply_intent_patch(valid_patch, repo)

    assert result["status"] == "REJECTED"
    assert result["validation_results"]["path_whitelist"] is False


def test_invalid_type_is_rejected(repo, fixture_loader):
    result = apply_intent_patch(fixture_loader("invalid_patch_bad_type.json"), repo)

    assert result["status"] == "REJECTED"
    assert result["validation_results"]["schema"] is False
