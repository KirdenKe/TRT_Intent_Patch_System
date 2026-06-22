from __future__ import annotations

from dataclasses import dataclass

from trt_core.chat_sessions import (
    clear_chat_session,
    load_chat_session,
    merge_pending_clarification,
    pending_clarification_state,
    resolve_priority_clarification_with_vllm,
    save_chat_session,
)


@dataclass
class FakeRepository:
    root: object


def test_file_backed_chat_session_preserves_pending_clarification(tmp_path):
    repository = FakeRepository(root=tmp_path)
    session_id = "chat/session:milestone-10"
    state = pending_clarification_state(
        session_id=session_id,
        intent_text=(
            "prioritize the adjustment of production lines 1 and 3 to focus on the ent surgical tooling set, "
            "and adjust the number of tooling on the production line so that only 6 remain"
        ),
        operator_id="op_001",
        reason="test for milestone 10",
        pending_question="Do you mean production-line priority, or should the robots on lines 1 and 3 pick ENT-required tools first?",
    )

    saved = save_chat_session(session_id, state, repository)
    loaded = load_chat_session(session_id, repository)
    merged = merge_pending_clarification(
        loaded["pending_intent"],
        "i mean the robots on lines 1 and 3 pick ENT-required tools first",
    )

    assert saved["state"] == "WAITING_FOR_CLARIFICATION"
    assert loaded["pending_intent"]["operator_id"] == "op_001"
    assert loaded["pending_intent"]["reason"] == "test for milestone 10"
    assert "Clarification:" in merged["merged_intent_text"]
    assert "ENT-required tools first" in merged["merged_intent_text"]
    assert merged["operator_id"] == "op_001"
    assert merged["reason"] == "test for milestone 10"


def test_clear_chat_session_returns_idle(tmp_path):
    repository = FakeRepository(root=tmp_path)
    save_chat_session(
        "session-1",
        pending_clarification_state(
            session_id="session-1",
            intent_text="pick required tools first",
            operator_id="op_001",
            reason="test",
            pending_question="clarify",
        ),
        repository,
    )

    cleared = clear_chat_session("session-1", repository)
    loaded = load_chat_session("session-1", repository)

    assert cleared["state"] == "IDLE"
    assert loaded["state"] == "IDLE"
    assert loaded["pending_intent"] is None


def priority_pending() -> dict:
    return {
        "original_intent_text": (
            "prioritize the adjustment of production lines 2 and 3 to focus on the ent surgical tooling set, "
            "and adjust the number of tooling on the production line so that only 5 remain"
        ),
        "intent_text": (
            "prioritize the adjustment of production lines 2 and 3 to focus on the ent surgical tooling set, "
            "and adjust the number of tooling on the production line so that only 5 remain"
        ),
        "operator_id": "op_001",
        "reason": "test for milestone 11",
        "pending_question": (
            "Do you mean production-line priority, or should the robots on lines 2 and 3 "
            "pick ENT-required tooling first?"
        ),
        "clarification_type": "PRODUCTION_PRIORITY_VS_ROBOT_REQUIRED_FIRST",
        "target_scope": "MULTIPLE_LINES",
        "target_lines": ["line_2", "line_3"],
        "target_set_id": "ENT_SURGICAL_TOOLING_SET",
        "simulation_config_updates": {"add_reference_number": 5},
    }


def fake_vllm_response(selected_option: str | None, resolved: bool = True, confidence: float = 0.9):
    def _post_json(url, body, timeout):
        assert "v1/chat/completions" in url
        assert body["structured_outputs"]["json"]["properties"]["selected_option"]["enum"] == [
            "PRODUCTION_LINE_PRIORITY",
            "ROBOT_REQUIRED_FIRST",
            None,
        ]
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"resolved": %s, "selected_option": %s, "confidence": %.1f, "reason": "fake"}'
                            % (
                                "true" if resolved else "false",
                                "null" if selected_option is None else f'"{selected_option}"',
                                confidence,
                            )
                        )
                    }
                }
            ]
        }

    return _post_json


def test_vllm_clarification_fallback_resolves_robot_required_first_and_preserves_lines():
    result = resolve_priority_clarification_with_vllm(
        priority_pending(),
        "yes i mean the robots on the lines 2 and 3 pick ent surgical tooling set first",
        {"lines": {f"line_{index}": {} for index in range(1, 5)}, "tool_sets": {"ENT_SURGICAL_TOOLING_SET": {}}},
        post_json=fake_vllm_response("ROBOT_REQUIRED_FIRST"),
    )

    assert result["resolved"] is True
    assert result["selected_option"] == "ROBOT_REQUIRED_FIRST"
    assert result["target_lines"] == ["line_2", "line_3"]
    assert result["target_scope"] == "MULTIPLE_LINES"
    assert result["target_set_id"] == "ENT_SURGICAL_TOOLING_SET"
    assert result["simulation_config_updates"] == {"add_reference_number": 5}


def test_vllm_clarification_fallback_resolves_production_line_priority():
    result = resolve_priority_clarification_with_vllm(
        priority_pending(),
        "I mean line scheduling priority",
        {"lines": {f"line_{index}": {} for index in range(1, 5)}, "tool_sets": {"ENT_SURGICAL_TOOLING_SET": {}}},
        post_json=fake_vllm_response("PRODUCTION_LINE_PRIORITY"),
    )

    assert result["resolved"] is True
    assert result["selected_option"] == "PRODUCTION_LINE_PRIORITY"


def test_vllm_clarification_fallback_can_remain_unresolved():
    result = resolve_priority_clarification_with_vllm(
        priority_pending(),
        "not sure",
        {"lines": {f"line_{index}": {} for index in range(1, 5)}, "tool_sets": {"ENT_SURGICAL_TOOLING_SET": {}}},
        post_json=fake_vllm_response(None, resolved=False, confidence=0.1),
    )

    assert result["resolved"] is False
    assert result["selected_option"] is None
