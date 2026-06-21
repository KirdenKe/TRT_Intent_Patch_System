from __future__ import annotations

from dataclasses import dataclass

from trt_core.chat_sessions import (
    clear_chat_session,
    load_chat_session,
    merge_pending_clarification,
    pending_clarification_state,
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
