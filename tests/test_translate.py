from __future__ import annotations

from codex_shim.translate import chat_completion_to_response, responses_to_anthropic, responses_to_chat


def test_responses_to_chat_text_input():
    body = {"model": "slug", "instructions": "System", "input": "Hello", "stream": True, "max_output_tokens": 99}
    out = responses_to_chat(body, "real-model")
    assert out["model"] == "real-model"
    assert out["stream"] is True
    assert out["max_tokens"] == 99
    assert out["messages"] == [{"role": "system", "content": "System"}, {"role": "user", "content": "Hello"}]


def test_responses_to_chat_preserves_reasoning_and_effort_for_deepseek():
    body = {
        "model": "slug",
        "reasoning_effort": "high",
        "input": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "prior thought"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "prior answer"}]},
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "next"}]},
        ],
    }

    out = responses_to_chat(body, "deepseek-reasoner")

    assert out["reasoning_effort"] == "high"
    assert out["messages"] == [
        {"role": "assistant", "content": "prior answer", "reasoning_content": "prior thought"},
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "next"},
    ]


def test_responses_to_chat_sanitizes_and_merges_strict_provider_messages():
    body = {
        "model": "slug",
        "instructions": "System\x00one",
        "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules\x00two"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi\x00"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "again\x01"}]},
            {"type": "function_call", "call_id": "call\x000", "name": "tool", "arguments": "{\"x\":\"y\x00\"}"},
        ],
    }

    out = responses_to_chat(body, "kimi-k2")

    assert out["messages"] == [
        {"role": "system", "content": "Systemone\n\nrulestwo"},
        {"role": "user", "content": "hi\n\nagain"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call0", "type": "function", "function": {"name": "tool", "arguments": "{\"x\":\"y\"}"}}
            ],
        },
    ]


def test_responses_function_tools_convert_to_chat_shape():
    body = {
        "model": "slug",
        "input": "Hi",
        "tools": [{"type": "function", "name": "do_work", "description": "Do work", "parameters": {"type": "object"}}],
    }
    out = responses_to_chat(body, "real-model")
    assert out["tools"] == [
        {
            "type": "function",
            "function": {"name": "do_work", "description": "Do work", "parameters": {"type": "object"}},
        }
    ]


def test_responses_to_anthropic_messages():
    body = {"model": "slug", "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hi"}]}]}
    out = responses_to_anthropic(body, "claude-real", 123)
    assert out["model"] == "claude-real"
    assert out["max_tokens"] == 123
    assert out["messages"] == [{"role": "user", "content": "Hi"}]


def test_chat_completion_to_response_strips_think():
    payload = {
        "id": "chatcmpl_1",
        "choices": [{"message": {"role": "assistant", "content": "<think>secret</think>Hello"}}],
    }
    out = chat_completion_to_response(payload, "slug")
    assert out["model"] == "slug"
    assert out["output"][0]["content"][0]["text"] == "Hello"
