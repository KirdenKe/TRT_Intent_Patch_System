"""File-backed chat session state for multi-turn operator dialogues."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHAT_SESSION_STATES = {
    "IDLE",
    "WAITING_FOR_REQUIRED_FIELDS",
    "WAITING_FOR_CLARIFICATION",
    "WAITING_FOR_APPROVAL_DECISION",
    "RUNNING_SIMULATION",
    "COMPLETED",
    "CANCELLED",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_session_id(session_id: str) -> str:
    value = str(session_id or "default").strip() or "default"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120] or "default"


def session_dir(repository: Any | None = None) -> Path:
    root = Path(getattr(repository, "root", "."))
    return root / "data" / "chat_sessions"


def session_path(session_id: str, repository: Any | None = None) -> Path:
    return session_dir(repository) / f"{safe_session_id(session_id)}.json"


def load_chat_session(session_id: str, repository: Any | None = None) -> dict[str, Any]:
    path = session_path(session_id, repository)
    if not path.exists():
        return {
            "session_id": safe_session_id(session_id),
            "state": "IDLE",
            "pending_intent": None,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_chat_session(session_id: str, state: dict[str, Any], repository: Any | None = None) -> dict[str, Any]:
    path = session_path(session_id, repository)
    path.parent.mkdir(parents=True, exist_ok=True)
    session = {
        **state,
        "session_id": safe_session_id(session_id),
        "updated_at": _now_utc(),
    }
    if session.get("state") not in CHAT_SESSION_STATES:
        raise ValueError(f"Unsupported chat session state: {session.get('state')}")
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(session, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)
    return session


def clear_chat_session(session_id: str, repository: Any | None = None) -> dict[str, Any]:
    path = session_path(session_id, repository)
    if path.exists():
        path.unlink()
    return {
        "session_id": safe_session_id(session_id),
        "state": "IDLE",
        "pending_intent": None,
        "cleared_at": _now_utc(),
    }


def pending_required_fields_state(
    *,
    session_id: str,
    intent_text: str,
    operator_id: str | None,
    reason: str | None,
    missing_fields: list[str],
) -> dict[str, Any]:
    return {
        "session_id": safe_session_id(session_id),
        "state": "WAITING_FOR_REQUIRED_FIELDS",
        "pending_intent": {
            "original_intent_text": intent_text,
            "intent_text": intent_text,
            "operator_id": operator_id,
            "reason": reason,
            "missing_fields": missing_fields,
            "pending_status": "WAITING_FOR_REQUIRED_FIELDS",
        },
    }


def pending_clarification_state(
    *,
    session_id: str,
    intent_text: str,
    operator_id: str | None,
    reason: str | None,
    pending_question: str,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "session_id": safe_session_id(session_id),
        "state": "WAITING_FOR_CLARIFICATION",
        "pending_intent": {
            "original_intent_text": intent_text,
            "intent_text": intent_text,
            "operator_id": operator_id,
            "reason": reason,
            "pending_question": pending_question,
            "pending_status": "WAITING_FOR_CLARIFICATION",
            "missing_or_unclear_fields": errors or [],
        },
    }


def merge_pending_clarification(pending_intent: dict[str, Any], clarification_text: str) -> dict[str, Any]:
    original = pending_intent.get("original_intent_text") or pending_intent.get("intent_text") or ""
    clarification = str(clarification_text or "").strip()
    merged = f"{original} Clarification: {clarification}".strip()
    return {
        "merged_intent_text": merged,
        "operator_id": pending_intent.get("operator_id"),
        "reason": pending_intent.get("reason"),
        "original_intent_text": original,
        "clarification_text": clarification,
    }
