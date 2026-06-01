from __future__ import annotations

import json
from pathlib import Path

from scenario_generation.chat_response_formatter import format_chat_response, normalize_chat_response


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "chat_formatter"
CANONICAL_FIXTURES = [
    "canonical_missing_from_errors.json",
    "canonical_missing_from_payload_raw_strings.json",
    "canonical_intent_text_present.json",
    "canonical_duplicate_missing_fields.json",
    "canonical_debug_false_hides_raw.json",
    "canonical_debug_true_includes_raw.json",
]


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_canonical_missing_field_extraction_fixtures():
    for name in CANONICAL_FIXTURES:
        fixture = load_fixture(name)
        canonical = normalize_chat_response(fixture["input"])

        assert canonical["missing_fields"] == fixture["expected"]["missing_fields"], name
        assert canonical["debug"] is fixture["expected"]["debug"], name
        if "intent_summary" in fixture["expected"]:
            assert canonical["intent_summary"] == fixture["expected"]["intent_summary"], name
        assert all(not field.startswith("Missing required") for field in canonical["missing_fields"])
        assert len(canonical["missing_fields"]) == len(set(canonical["missing_fields"]))
        if canonical["intent_summary"]:
            assert "intent_text" not in canonical["missing_fields"]


def test_debug_false_hides_raw_backend_response():
    canonical = normalize_chat_response(load_fixture("canonical_debug_false_hides_raw.json")["input"])
    output = format_chat_response(load_fixture("canonical_debug_false_hides_raw.json")["input"])

    assert canonical["raw_backend_response"] is None
    assert output["debug_json"] is None


def test_debug_true_includes_debug_json():
    raw = load_fixture("canonical_debug_true_includes_raw.json")["input"]
    canonical = normalize_chat_response(raw)
    output = format_chat_response(raw)

    assert canonical["raw_backend_response"] == raw
    assert output["debug_json"]["raw_backend_response"] == raw


def test_malformed_llm_output_uses_fallback_template():
    fixture = load_fixture("canonical_malformed_llm_output.json")
    output = format_chat_response(fixture["input"], fixture["llm_output"])

    assert output["required_fields"] == ["operator_id", "reason"]
    assert output["next_action"] == "PROVIDE_MISSING_FIELDS"
    assert "Missing required chat field" not in output["user_message"]
    assert "Missing required chat field" not in output["suggested_reply"]
    assert "operator_id: op_001" in output["suggested_reply"]
    assert "reason: urgent trauma set deadline" in output["suggested_reply"]
    assert "{" not in output["user_message"]


def test_valid_llm_wording_keeps_canonical_required_fields():
    raw = load_fixture("canonical_missing_from_errors.json")["input"]
    output = format_chat_response(
        raw,
        {
            "user_message": "I need the operator ID and reason before review.",
            "next_action": "PROVIDE_MISSING_FIELDS",
            "required_fields": ["bad_llm_field"],
            "suggested_reply": "operator_id: op_001\nreason: urgent trauma set deadline",
            "debug_json": {"should_not_survive": True},
        },
    )

    assert output["required_fields"] == ["operator_id", "reason"]
    assert output["debug_json"] is None
