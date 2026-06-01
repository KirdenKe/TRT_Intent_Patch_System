"""Deterministic chat response normalization and post-checks."""

from __future__ import annotations

import re
from typing import Any


CANONICAL_STATUSES = {
    "NEEDS_CLARIFICATION",
    "REVIEWED",
    "RELEASED",
    "REJECTED",
    "NEEDS_REVISION",
    "WAITING",
    "WAITING_FOR_CHECKPOINT",
    "GENERATED",
    "ERROR",
}
NEXT_ACTIONS = {"PROVIDE_MISSING_FIELDS", "CONFIRM_PATCH", "REVISE_REQUEST", "WAIT", "DONE", "ERROR"}
MISSING_FIELD_RE = re.compile(r"Missing required chat field: ([a-zA-Z0-9_]+)")
MISSING_FIELD_WHITELIST = ["operator_id", "intent_text", "reason", "confirmation", "release_id", "patch_id"]
FIELD_LABELS = {
    "operator_id": "operator ID",
    "intent_text": "operator intent",
    "reason": "reason for the change",
    "confirmation": "confirmation decision",
    "release_id": "release ID",
    "patch_id": "patch ID",
}


def normalize_chat_response(raw: dict[str, Any]) -> dict[str, Any]:
    payload = raw.get("payload") or {}
    errors = raw.get("errors") or []
    context = raw.get("context") or {}
    debug = bool(raw.get("debug") or context.get("debug") or payload.get("debug"))
    intent_summary = _clean_intent_summary(payload.get("intent_text") or payload.get("raw_chat_input") or "")
    missing_fields = _canonical_missing_fields(payload, errors, intent_summary)
    return {
        "status": _canonical_status(raw.get("status")),
        "intent_summary": intent_summary,
        "missing_fields": missing_fields,
        "rejection_reasons": _rejection_reasons(payload, errors),
        "field_labels": FIELD_LABELS,
        "example": {
            "operator_id": "op_001",
            "reason": "urgent trauma set deadline",
        },
        "ids": _extract_ids(raw),
        "debug": debug,
        "raw_backend_response": raw if debug else None,
    }


def post_check_formatter_output(canonical: dict[str, Any], llm_output: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(llm_output, dict):
        return _fallback_response(canonical)
    output = {
        "user_message": str(llm_output.get("user_message", "")),
        "next_action": llm_output.get("next_action"),
        "required_fields": list(canonical["missing_fields"]),
        "suggested_reply": str(llm_output.get("suggested_reply", "")),
        "debug_json": canonical if canonical.get("debug") else None,
    }
    if not _valid_output(canonical, output):
        return _fallback_response(canonical)
    return output


def format_chat_response(raw: dict[str, Any], llm_output: dict[str, Any] | None = None) -> dict[str, Any]:
    canonical = normalize_chat_response(raw)
    return post_check_formatter_output(canonical, llm_output or _deterministic_draft(canonical))


def _canonical_status(value: Any) -> str:
    status = str(value or "ERROR")
    if status == "WAITING_FOR_CHECKPOINT":
        return "WAITING"
    return status if status in CANONICAL_STATUSES else "ERROR"


def _canonical_missing_fields(payload: dict[str, Any], errors: list[Any], intent_summary: str) -> list[str]:
    source_values: list[Any] = []
    if "missing_canonical_fields" in payload:
        source_values.extend(_as_list(payload["missing_canonical_fields"]))
    else:
        source_values.extend(_as_list(payload.get("missing_fields")))
        source_values.extend(_as_list(errors))

    fields: list[str] = []
    for value in source_values:
        text = str(value)
        match = MISSING_FIELD_RE.search(text)
        field = match.group(1) if match else text
        if field == "intent_text" and intent_summary:
            continue
        if field in MISSING_FIELD_WHITELIST and field not in fields:
            fields.append(field)
    return fields


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _clean_intent_summary(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\.{2,}$", ".", text)
    text = re.sub(r"([!?])\1+$", r"\1", text)
    return text


def _extract_ids(raw: dict[str, Any]) -> dict[str, Any]:
    context = raw.get("context") or {}
    ids = raw.get("ids") or {}
    return {
        key: ids.get(key) if ids.get(key) not in {None, ""} else context.get(key)
        for key in ["patch_id", "release_id", "audit_id", "trt_id", "trt_version", "reconciliation_plan_id"]
    }


def _rejection_reasons(payload: dict[str, Any], errors: list[Any]) -> list[str]:
    values = _as_list(payload.get("rejection_reasons")) + _as_list(errors)
    reasons: list[str] = []
    for value in values:
        reason = str(value).strip()
        if not reason or MISSING_FIELD_RE.search(reason):
            continue
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def _deterministic_draft(canonical: dict[str, Any]) -> dict[str, Any]:
    status = canonical["status"]
    if status == "NEEDS_CLARIFICATION":
        return _fallback_response(canonical)
    if status == "REVIEWED":
        return {
            "user_message": "Candidate patch reviewed successfully. Please approve, reject, or request revision.",
            "next_action": "CONFIRM_PATCH",
            "required_fields": [],
            "suggested_reply": "Reply with APPROVE, REJECT, or REQUEST_REVISION.",
            "debug_json": canonical if canonical.get("debug") else None,
        }
    if status == "RELEASED":
        ids = canonical["ids"]
        return {
            "user_message": f"Release completed. Release ID: {ids.get('release_id')}. TRT version: {ids.get('trt_version')}. Audit ID: {ids.get('audit_id')}.",
            "next_action": "WAIT",
            "required_fields": [],
            "suggested_reply": "Wait for reconciliation and ScenarioSpec generation results.",
            "debug_json": canonical if canonical.get("debug") else None,
        }
    if status in {"REJECTED", "NEEDS_REVISION"}:
        reasons = canonical.get("rejection_reasons") or []
        reason_text = f": {'; '.join(reasons)}" if reasons else ""
        return {
            "user_message": f"The request cannot continue yet{reason_text}. Please revise the request.",
            "next_action": "REVISE_REQUEST",
            "required_fields": [],
            "suggested_reply": "Reply with a revised operator intent and reason.",
            "debug_json": canonical if canonical.get("debug") else None,
        }
    return {
        "user_message": f"Workflow status: {status}.",
        "next_action": "DONE" if status == "GENERATED" else "WAIT",
        "required_fields": [],
        "suggested_reply": "No further chat action is required." if status == "GENERATED" else "Wait for the next workflow step.",
        "debug_json": canonical if canonical.get("debug") else None,
    }


def _fallback_response(canonical: dict[str, Any]) -> dict[str, Any]:
    if canonical["status"] == "NEEDS_CLARIFICATION":
        bullets = "\n".join(f"- {canonical['field_labels'][field]}" for field in canonical["missing_fields"])
        reply_lines = "\n".join(
            f"{field}: {canonical['example'].get(field, '<value>')}" for field in canonical["missing_fields"]
        )
        return {
            "user_message": (
                f"I understood your request as: {canonical['intent_summary']}\n\n"
                f"Before I can submit this for review, I still need:\n{bullets}\n\n"
                f"You can reply with:\n{reply_lines}"
            ),
            "next_action": "PROVIDE_MISSING_FIELDS",
            "required_fields": list(canonical["missing_fields"]),
            "suggested_reply": reply_lines,
            "debug_json": canonical if canonical.get("debug") else None,
        }
    return _deterministic_draft({**canonical, "debug": canonical.get("debug", False)})


def _valid_output(canonical: dict[str, Any], output: dict[str, Any]) -> bool:
    if output["next_action"] not in NEXT_ACTIONS:
        return False
    if canonical["status"] == "NEEDS_CLARIFICATION" and output["next_action"] != "PROVIDE_MISSING_FIELDS":
        return False
    if output["required_fields"] != canonical["missing_fields"]:
        return False
    if any(field not in MISSING_FIELD_WHITELIST for field in output["required_fields"]):
        return False
    if len(output["required_fields"]) != len(set(output["required_fields"])):
        return False
    if any(str(field).startswith("Missing required") for field in output["required_fields"]):
        return False
    if "Missing required chat field" in output["user_message"]:
        return False
    if "{" in output["user_message"] or "}" in output["user_message"]:
        return False
    if "Missing required chat field" in output["suggested_reply"]:
        return False
    return True
