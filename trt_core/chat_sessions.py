"""File-backed chat session state for multi-turn operator dialogues."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHAT_SESSION_STATES = {
    "IDLE",
    "WAITING_FOR_REQUIRED_FIELDS",
    "WAITING_FOR_CLARIFICATION",
    "WAITING_FOR_APPROVAL",
    "WAITING_FOR_APPROVAL_DECISION",
    "WAITING_FOR_DEPLOYMENT_DECISION",
    "WAITING_FOR_POST_EVIDENCE_DECISION",
    "RUNNING_SIMULATION",
    "COMPLETED",
    "CANCELLED",
}

DEFAULT_VLLM_CHAT_COMPLETIONS_URL = "http://192.168.50.168:29987/v1/chat/completions"
DEFAULT_VLLM_MODEL = "cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit"


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


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower().replace("-", " ")).strip()


def _line_ids_from_text(value: str) -> list[str]:
    text = _normalize_text(value)
    if "line" not in text:
        return []
    return [f"line_{number}" for number in dict.fromkeys(re.findall(r"\b([1-4])\b", text))]


def _all_line_ids(current_trt: dict[str, Any] | None) -> list[str]:
    lines = sorted((current_trt or {}).get("lines", {}))
    return lines or ["line_1", "line_2", "line_3", "line_4"]


def _is_all_lines_text(value: str) -> bool:
    text = _normalize_text(value)
    return any(
        phrase in text
        for phrase in (
            "all lines",
            "all production lines",
            "every line",
            "every production line",
            "each line",
            "each production line",
            "all robots",
        )
    )


def _add_reference_number_from_pending(pending_intent: dict[str, Any], reply_text: str) -> int | None:
    for source in (
        pending_intent.get("simulation_config_updates"),
        (pending_intent.get("partial_resolution") or {}).get("simulation_config_updates"),
    ):
        if isinstance(source, dict) and source.get("add_reference_number") is not None:
            return int(source["add_reference_number"])
    text = " ".join(
        str(value or "")
        for value in (
            pending_intent.get("original_intent_text"),
            pending_intent.get("intent_text"),
            reply_text,
        )
    ).lower()
    match = re.search(r"\badd_reference_number\b[^\d]*(\d+)\b", text)
    if not match:
        match = re.search(r"\bonly\s+(\d+)\s+(?:remain|tool|tools|tooling)\b", text)
    if not match:
        match = re.search(r"\b(\d+)\s+(?:remain|tools?\s+remain|tooling\s+remain)\b", text)
    return int(match.group(1)) if match else None


def _robot_required_first_resolution(
    pending_intent: dict[str, Any],
    reply_text: str,
    current_trt: dict[str, Any] | None = None,
    *,
    vllm_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    original = str(pending_intent.get("original_intent_text") or pending_intent.get("intent_text") or "")
    if _is_all_lines_text(reply_text) or _is_all_lines_text(original):
        target_scope = "ALL_LINES"
        target_lines = _all_line_ids(current_trt)
    else:
        target_lines = (
            _line_ids_from_text(reply_text)
            or list((pending_intent.get("partial_resolution") or {}).get("target_lines") or [])
            or list(pending_intent.get("target_lines") or [])
            or _line_ids_from_text(original)
        )
        target_scope = (
            (pending_intent.get("partial_resolution") or {}).get("target_scope")
            or pending_intent.get("target_scope")
            or ("MULTIPLE_LINES" if len(target_lines) > 1 else "SINGLE_LINE")
        )
    target_set_id = (
        pending_intent.get("target_set_id")
        or (pending_intent.get("partial_resolution") or {}).get("target_set_id")
        or ("ENT_SURGICAL_TOOLING_SET" if "ENT_SURGICAL_TOOLING_SET" in (current_trt or {}).get("tool_sets", {}) else None)
    )
    simulation_config_updates: dict[str, Any] = {}
    add_reference_number = _add_reference_number_from_pending(pending_intent, reply_text)
    if add_reference_number is not None:
        simulation_config_updates["add_reference_number"] = add_reference_number
    request_types = ["TOOLING_POLICY_UPDATE", "MANIPULATOR_PRIORITY_UPDATE"]
    if simulation_config_updates:
        request_types.append("SIMULATION_CONFIG_UPDATE")
    merged_intent = (
        f"Set {', '.join(target_lines) if target_scope != 'ALL_LINES' else 'all production lines'} "
        "to target the ENT surgical tooling set and make their robots pick ENT-required tooling first."
    )
    if add_reference_number is not None:
        merged_intent += f" Set the simulated tooling count to {add_reference_number}."
    resolution = {
        "clarification_resolved": True,
        "resolved": True,
        "selected_option": "ROBOT_REQUIRED_FIRST",
        "intent_text": merged_intent,
        "target_scope": target_scope,
        "target_lines": target_lines,
        "target_set_id": target_set_id,
        "request_types": request_types,
        "detected_request_types": request_types,
        "manipulator_priority": {
            "policy": "REQUIRED_FIRST",
            "enabled": True,
            "tie_breaker": "FCFS",
            "ordered_tool_ids": [],
            "ordered_normalized_types": [],
        },
        "simulation_config_updates": simulation_config_updates,
        "operator_id": pending_intent.get("operator_id"),
        "reason": pending_intent.get("reason"),
        "original_intent_text": original,
        "clarification_text": str(reply_text or "").strip(),
    }
    if vllm_resolution:
        resolution["vllm_resolution"] = vllm_resolution
    return resolution


def resolve_pending_priority_clarification(
    pending_intent: dict[str, Any],
    reply_text: str,
    current_trt: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve the closed-choice production-priority vs robot-required-first clarification."""

    pending_question = _normalize_text(pending_intent.get("pending_question") or "")
    clarification_type = pending_intent.get("clarification_type")
    is_priority_choice = clarification_type == "PRODUCTION_PRIORITY_VS_ROBOT_REQUIRED_FIRST" or (
        "production-line priority" in pending_question and "pick ent-required" in pending_question
    )
    if not is_priority_choice:
        return None

    text = _normalize_text(reply_text)
    robot_required_first_phrases = [
        "robots on",
        "robot on",
        "pick ent required",
        "pick ent surgical",
        "pick ent tools",
        "pick ent required",
        "pick required",
        "required tooling first",
        "required ent tools",
        "ent required tooling first",
        "ent surgical tooling first",
        "ent surgical tools first",
        "ent tools first",
        "ent required tooling first",
        "pick ent required tooling first",
        "pick ent surgical tooling first",
        "pick ent surgical tools first",
    ]
    production_priority_phrases = [
        "production-line priority",
        "production line priority",
        "line priority",
        "priority level",
        "schedule priority",
        "prioritize the line",
    ]
    robot_score = sum(1 for phrase in robot_required_first_phrases if phrase in text)
    production_score = sum(1 for phrase in production_priority_phrases if phrase in text)

    if robot_score <= 0 and production_score <= 0 and all(term in text for term in ("robot", "pick", "required")):
        robot_score = 1
    if robot_score <= 0 and production_score <= 0:
        has_ent_or_required = "ent" in text or "required tooling" in text or "required tools" in text
        has_pick_or_robot = any(term in text for term in ("robot", "robots", "pick", "picking", "prioritize", "make"))
        has_first = "first" in text
        if has_ent_or_required and has_first and (has_pick_or_robot or "tooling first" in text or "tools first" in text):
            robot_score = 1

    if robot_score > 0 and production_score == 0:
        return _robot_required_first_resolution(pending_intent, reply_text, current_trt)

    if production_score > 0 and robot_score == 0:
        return {
            "clarification_resolved": True,
            "resolved": True,
            "selected_option": "PRODUCTION_LINE_PRIORITY",
            "operator_id": pending_intent.get("operator_id"),
            "reason": pending_intent.get("reason"),
            "original_intent_text": pending_intent.get("original_intent_text") or pending_intent.get("intent_text") or "",
            "clarification_text": str(reply_text or "").strip(),
        }

    if int(pending_intent.get("clarification_ask_count") or 1) >= 1 and all(
        term in text for term in ("robot", "pick", "required")
    ):
        return {
            "clarification_resolved": False,
            "resolved": False,
            "error_code": "CLARIFICATION_LOOP_DETECTED",
            "message": "The operator answered the priority clarification, but the workflow attempted to ask the same clarification again.",
        }

    return None


def _post_vllm_json(url: str, body: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def resolve_priority_clarification_with_vllm(
    pending_intent: dict[str, Any],
    reply_text: str,
    current_trt: dict[str, Any] | None = None,
    *,
    post_json: Any | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    pending_question = str(pending_intent.get("pending_question") or "")
    clarification_type = pending_intent.get("clarification_type")
    if clarification_type != "PRODUCTION_PRIORITY_VS_ROBOT_REQUIRED_FIRST" and "production-line priority" not in pending_question:
        return {"resolved": False, "selected_option": None, "confidence": 0.0, "reason": "Pending clarification is not a priority closed-choice question."}

    original = str(pending_intent.get("original_intent_text") or pending_intent.get("intent_text") or "")
    target_lines = (
        list((pending_intent.get("partial_resolution") or {}).get("target_lines") or [])
        or list(pending_intent.get("target_lines") or [])
        or _line_ids_from_text(original)
    )
    target_scope = (
        (pending_intent.get("partial_resolution") or {}).get("target_scope")
        or pending_intent.get("target_scope")
        or ("ALL_LINES" if _is_all_lines_text(original) else ("MULTIPLE_LINES" if len(target_lines) > 1 else "SINGLE_LINE"))
    )
    if target_scope == "ALL_LINES":
        target_lines = _all_line_ids(current_trt)
    target_set_id = (
        pending_intent.get("target_set_id")
        or (pending_intent.get("partial_resolution") or {}).get("target_set_id")
        or "ENT_SURGICAL_TOOLING_SET"
    )
    simulation_config_updates = (
        pending_intent.get("simulation_config_updates")
        or (pending_intent.get("partial_resolution") or {}).get("simulation_config_updates")
        or {}
    )
    resolver_input = {
        "clarification_type": "PRODUCTION_PRIORITY_VS_ROBOT_REQUIRED_FIRST",
        "pending_question": pending_question,
        "operator_reply": str(reply_text or ""),
        "original_intent_text": original,
        "target_scope": target_scope,
        "target_lines": target_lines,
        "target_set_id": target_set_id,
        "simulation_config_updates": simulation_config_updates,
        "allowed_options": [
            {"id": "PRODUCTION_LINE_PRIORITY", "meaning": "Change scheduling or line priority only."},
            {"id": "ROBOT_REQUIRED_FIRST", "meaning": "Make the robots pick ENT-required tooling before non-ENT tooling."},
        ],
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["resolved", "selected_option", "confidence", "reason"],
        "properties": {
            "resolved": {"type": "boolean"},
            "selected_option": {"type": ["string", "null"], "enum": ["PRODUCTION_LINE_PRIORITY", "ROBOT_REQUIRED_FIRST", None]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
    }
    body = {
        "model": os.getenv("VLLM_MODEL", DEFAULT_VLLM_MODEL),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You resolve a user's answer to a prior closed-choice clarification question. "
                    "You must choose only from the allowed options. Do not generate a patch. "
                    "Do not invent target lines. Preserve the original target scope. "
                    "If the user says the robots should pick ENT surgical tools first, ENT-required tools first, "
                    "required tools first, or ENT set first, choose ROBOT_REQUIRED_FIRST. "
                    "If the user says line priority, schedule priority, or production-line priority, choose PRODUCTION_LINE_PRIORITY. "
                    "If unclear, return resolved=false."
                ),
            },
            {"role": "user", "content": json.dumps(resolver_input, sort_keys=True)},
        ],
        "temperature": 0,
        "max_tokens": 2000,
        "structured_outputs": {"json": schema},
    }
    url = os.getenv("VLLM_CHAT_COMPLETIONS_URL", DEFAULT_VLLM_CHAT_COMPLETIONS_URL)
    timeout = float(timeout_seconds if timeout_seconds is not None else os.getenv("VLLM_CLARIFICATION_TIMEOUT_SECONDS", "10"))
    try:
        raw = (post_json or _post_vllm_json)(url, body, timeout)
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", raw)
        resolved = json.loads(content) if isinstance(content, str) else content
    except (OSError, TimeoutError, urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
        return {"resolved": False, "selected_option": None, "confidence": 0.0, "reason": f"vLLM clarification resolver failed: {exc}"}

    selected = resolved.get("selected_option")
    confidence = float(resolved.get("confidence") or 0.0)
    if resolved.get("resolved") is True and selected == "ROBOT_REQUIRED_FIRST" and confidence >= 0.5:
        return _robot_required_first_resolution(
            pending_intent,
            reply_text,
            current_trt,
            vllm_resolution={
                "resolved": True,
                "selected_option": selected,
                "confidence": confidence,
                "reason": str(resolved.get("reason") or ""),
            },
        )
    if resolved.get("resolved") is True and selected == "PRODUCTION_LINE_PRIORITY" and confidence >= 0.5:
        return {
            "clarification_resolved": True,
            "resolved": True,
            "selected_option": "PRODUCTION_LINE_PRIORITY",
            "operator_id": pending_intent.get("operator_id"),
            "reason": pending_intent.get("reason"),
            "original_intent_text": original,
            "clarification_text": str(reply_text or "").strip(),
            "vllm_resolution": {
                "resolved": True,
                "selected_option": selected,
                "confidence": confidence,
                "reason": str(resolved.get("reason") or ""),
            },
        }
    return {
        "resolved": False,
        "selected_option": selected,
        "confidence": confidence,
        "reason": str(resolved.get("reason") or "vLLM did not resolve the closed-choice clarification."),
        "vllm_resolution": resolved,
    }

