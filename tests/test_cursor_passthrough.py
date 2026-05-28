from __future__ import annotations

import json

from codex_shim.cursor_passthrough import (
    CursorStreamParser,
    build_cursor_prompt,
    is_cursor_passthrough_slug,
)


def test_is_cursor_passthrough_slug():
    assert is_cursor_passthrough_slug("composer-2-5")
    assert is_cursor_passthrough_slug("composer-2.5")
    assert not is_cursor_passthrough_slug("gpt-5.5")


def test_build_cursor_prompt_from_responses_body():
    body = {
        "model": "composer-2-5",
        "instructions": "You are Codex.",
        "input": [{"role": "user", "content": "Hello"}],
    }
    prompt = build_cursor_prompt(body)
    assert "You are Codex." in prompt
    assert "Hello" in prompt


def test_cursor_stream_parser_emits_deltas():
    parser = CursorStreamParser()
    line1 = json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hel"}]},
            "timestamp_ms": 1,
        }
    )
    line2 = json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
            "timestamp_ms": 2,
        }
    )
    assert parser.feed_line(line1) == "Hel"
    assert parser.feed_line(line2) == "lo"
