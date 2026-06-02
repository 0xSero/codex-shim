from __future__ import annotations

from codex_shim.translate import (
    anthropic_messages_to_chat,
    anthropic_to_response,
    chat_completion_to_anthropic_message,
    chat_completion_to_response,
    responses_to_anthropic,
    responses_to_chat,
)


def test_responses_to_chat_text_input():
    body = {"model": "slug", "instructions": "System", "input": "Hello", "stream": True, "max_output_tokens": 99}
    out = responses_to_chat(body, "real-model")
    assert out["model"] == "real-model"
    assert out["stream"] is True
    assert out["max_tokens"] == 99
    assert out["messages"] == [{"role": "system", "content": "System"}, {"role": "user", "content": "Hello"}]


def test_anthropic_messages_to_chat_preserves_tools_images_and_tool_results():
    body = {
        "model": "slug",
        "system": [{"type": "text", "text": "System"}],
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "repo"}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {"type": "text", "text": "result"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "BBB"}},
                        ],
                    },
                    {"type": "text", "text": "Next"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
                ],
            },
        ],
        "max_tokens": 99,
        "stop_sequences": ["END"],
        "stream": True,
        "tools": [{"name": "lookup", "description": "Lookup", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "tool", "name": "lookup"},
    }

    out = anthropic_messages_to_chat(body, "real-model")

    assert out["model"] == "real-model"
    assert out["max_tokens"] == 99
    assert out["stop"] == ["END"]
    assert out["stream_options"] == {"include_usage": True}
    assert out["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}
    assert out["tools"] == [
        {
            "type": "function",
            "function": {"name": "lookup", "description": "Lookup", "parameters": {"type": "object"}},
        }
    ]
    assert out["messages"] == [
        {"role": "system", "content": "System"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "toolu_1", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"repo\"}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "result\n[image]"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Visual tool result for toolu_1."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBB"}},
                {"type": "text", "text": "Next"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        },
    ]


def test_chat_completion_to_anthropic_message_preserves_text_tools_and_usage():
    payload = {
        "id": "chatcmpl_fake",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "hello",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"repo\"}"}},
                        {"id": "call_2", "type": "function", "function": {"name": "broken", "arguments": "{"}},
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }

    out = chat_completion_to_anthropic_message(payload, "shim-model")

    assert out["id"] == "chatcmpl_fake"
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["model"] == "shim-model"
    assert out["stop_reason"] == "tool_use"
    assert out["usage"] == {"input_tokens": 4, "output_tokens": 2}
    assert out["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "repo"}},
        {"type": "tool_use", "id": "call_2", "name": "broken", "input": {"_raw": "{"}},
    ]



def test_chat_completion_to_anthropic_message_tool_only_uses_empty_content():
    payload = {
        "id": "chatcmpl_tool_only",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"repo\"}"}},
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }

    out = chat_completion_to_anthropic_message(payload, "shim-model")

    assert out["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "repo"}},
    ]
    assert out["stop_reason"] == "tool_use"


def test_chat_completion_to_anthropic_message_includes_reasoning():
    payload = {
        "id": "chatcmpl_reason",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning_content": "thinking hard",
                },
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }

    out = chat_completion_to_anthropic_message(payload, "shim-model")

    assert out["content"] == [
        {"type": "thinking", "thinking": "thinking hard"},
        {"type": "text", "text": "answer"},
    ]

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


def test_native_responses_tools_get_function_fallbacks_for_byok_chat():
    body = {
        "model": "slug",
        "input": "Use the browser",
        "tool_choice": {"type": "computer_use_preview"},
        "tools": [
            {"type": "computer_use_preview"},
            {"type": "web_search_preview"},
            {"type": "apply_patch"},
            {"type": "function", "name": "list_mcp_resources", "parameters": {"type": "object"}},
        ],
    }

    out = responses_to_chat(body, "real-model")

    functions = [tool["function"] for tool in out["tools"]]
    assert [fn["name"] for fn in functions] == ["computer_use", "web_search", "apply_patch", "list_mcp_resources"]
    assert functions[0]["parameters"]["required"] == ["action"]
    assert functions[1]["parameters"]["required"] == ["query"]
    assert functions[2]["parameters"]["required"] == ["patch"]
    assert out["tool_choice"] == {"type": "function", "function": {"name": "computer_use"}}


def test_native_responses_tools_get_anthropic_fallbacks():
    body = {
        "model": "slug",
        "input": "Search",
        "tools": [{"type": "web_search_preview"}, {"type": "computer_use_preview"}],
    }

    out = responses_to_anthropic(body, "claude-real", 123)

    assert [tool["name"] for tool in out["tools"]] == ["web_search", "computer_use"]
    assert out["tools"][0]["input_schema"]["required"] == ["query"]
    assert out["tools"][1]["input_schema"]["required"] == ["action"]


def test_responses_to_anthropic_messages():
    body = {"model": "slug", "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hi"}]}]}
    out = responses_to_anthropic(body, "claude-real", 123)
    assert out["model"] == "claude-real"
    assert out["max_tokens"] == 123
    assert out["messages"] == [{"role": "user", "content": "Hi"}]


def test_responses_to_chat_preserves_input_images_for_vision_models():
    body = {
        "model": "slug",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What is visible?"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AAA", "detail": "high"},
                ],
            }
        ],
    }

    out = responses_to_chat(body, "vision-model")

    assert out["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is visible?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA", "detail": "high"}},
            ],
        }
    ]


def test_computer_call_output_screenshot_reaches_openai_chat_vision():
    body = {
        "model": "slug",
        "input": [
            {"type": "computer_call_output", "call_id": "cu_1", "output": {"type": "input_image", "image_url": "data:image/png;base64,BBB"}}
        ],
    }

    out = responses_to_chat(body, "vision-model")

    assert out["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Computer output for cu_1."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB"}},
            ],
        }
    ]


def test_function_call_output_visual_feedback_adds_followup_image_message():
    body = {
        "model": "slug",
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "computer_use", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": [{"type": "input_image", "image_url": "data:image/png;base64,CCC"}]},
        ],
    }

    out = responses_to_chat(body, "vision-model")

    assert out["messages"][1] == {"role": "tool", "tool_call_id": "call_1", "content": "[image]"}
    assert out["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "Visual tool output for call_1."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,CCC"}},
        ],
    }


def test_responses_to_anthropic_preserves_visual_feedback_as_image_blocks():
    body = {
        "model": "slug",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Inspect this."},
                    {"type": "input_image", "image_url": "data:image/png;base64,DDD"},
                ],
            },
            {"type": "computer_call_output", "call_id": "cu_2", "output": {"type": "input_image", "image_url": "https://example.invalid/screen.png"}},
        ],
    }

    out = responses_to_anthropic(body, "claude-real", 123)

    assert out["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect this."},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "DDD"}},
                {"type": "text", "text": "Computer output for cu_2."},
                {"type": "image", "source": {"type": "url", "url": "https://example.invalid/screen.png"}},
            ],
        }
    ]


def test_chat_completion_to_response_strips_think():
    payload = {
        "id": "chatcmpl_1",
        "choices": [{"message": {"role": "assistant", "content": "<think>secret</think>Hello"}}],
    }
    out = chat_completion_to_response(payload, "slug")
    assert out["model"] == "slug"
    assert out["output"][0]["content"][0]["text"] == "Hello"


def test_chat_completion_to_response_normalizes_cached_usage():
    payload = {
        "id": "chatcmpl_1",
        "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 8},
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    }

    out = chat_completion_to_response(payload, "slug")

    assert out["usage"] == {
        "input_tokens": 10,
        "output_tokens": 2,
        "total_tokens": 12,
        "input_tokens_details": {"cached_tokens": 8},
        "output_tokens_details": {"reasoning_tokens": 1},
    }


def test_anthropic_to_response_normalizes_cache_usage():
    payload = {
        "id": "msg_1",
        "content": [{"type": "text", "text": "Hello"}],
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 8,
            "cache_creation_input_tokens": 2,
            "output_tokens": 3,
        },
    }

    out = anthropic_to_response(payload, "slug")

    assert out["usage"] == {
        "input_tokens": 10,
        "output_tokens": 3,
        "total_tokens": 13,
        "input_tokens_details": {
            "cached_tokens": 8,
            "cache_read_input_tokens": 8,
            "cache_creation_input_tokens": 2,
        },
    }
