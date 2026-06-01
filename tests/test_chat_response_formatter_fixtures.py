from __future__ import annotations

import json
from pathlib import Path

from scenario_generation.chat_response_formatter import format_chat_response


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "chat_formatter"


def test_chat_response_formatter_fixtures():
    for path in sorted(path for path in FIXTURE_DIR.glob("*.json") if not path.name.startswith("canonical_")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        output = format_chat_response(fixture["input"])
        expected = fixture["expected"]

        assert output["next_action"] == expected["next_action"], path.name
        assert output["required_fields"] == expected["required_fields"], path.name
        for text in expected["user_message_contains"]:
            assert text in output["user_message"], path.name
        assert (output["debug_json"] is not None) is expected["raw_json_present"], path.name
