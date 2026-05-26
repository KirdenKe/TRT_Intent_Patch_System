from __future__ import annotations

from trt_core.patch_apply import apply_intent_patch


def test_valid_patch_is_accepted_and_creates_new_trt_version(repo, valid_patch):
    result = apply_intent_patch(valid_patch, repo)
    current = repo.get_current_trt("trt-demo")

    assert result["status"] == "ACCEPTED"
    assert result["trt_version"] == "v2"
    assert current["version"] == "v2"
    assert current["lines"]["line_1"]["goal"] == "TRAUMA_SET_PRIORITY"
    assert len(repo.list_trt_versions("trt-demo")) == 2


def test_patch_to_readonly_state_field_is_rejected(repo, valid_patch):
    valid_patch["operations"] = [
        {
            "op": "replace",
            "path": "/lines/line_1/state/mode",
            "value": "PAUSED"
        }
    ]

    result = apply_intent_patch(valid_patch, repo)

    assert result["status"] == "REJECTED"
    assert result["validation_results"]["readonly"] is False


def test_stale_base_version_is_rejected(repo, valid_patch):
    first = apply_intent_patch(valid_patch, repo)
    stale_patch = dict(valid_patch)
    stale_patch["patch_id"] = "patch-stale-001"

    second = apply_intent_patch(stale_patch, repo)

    assert first["status"] == "ACCEPTED"
    assert second["status"] == "REJECTED"
    assert second["validation_results"]["base_version"] is False


def test_unsupported_move_operation_is_rejected(repo, valid_patch):
    valid_patch["operations"] = [
        {
            "op": "move",
            "from": "/lines/line_1/priority",
            "path": "/lines/line_2/priority"
        }
    ]

    result = apply_intent_patch(valid_patch, repo)

    assert result["status"] == "REJECTED"
    assert result["validation_results"]["path_whitelist"] is False


def test_unsupported_copy_operation_is_rejected(repo, valid_patch):
    valid_patch["operations"] = [
        {
            "op": "copy",
            "from": "/lines/line_1/priority",
            "path": "/lines/line_2/priority"
        }
    ]

    result = apply_intent_patch(valid_patch, repo)

    assert result["status"] == "REJECTED"
    assert result["validation_results"]["path_whitelist"] is False
