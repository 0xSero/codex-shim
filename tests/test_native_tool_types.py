"""Tests for native tool type mapping in the streaming translator and non-streaming response helpers."""
from __future__ import annotations

import json
from codex_shim.translate import chat_completion_to_response, anthropic_to_response
from codex_shim.server import _build_tool_types


def test_build_tool_types_native_tools():
    """Native tools like apply_patch and web_search are preserved."""
    body = {
        "tools": [
            {"type": "apply_patch"},
            {"type": "web_search_preview"},
            {"type": "local_shell"},
        ]
    }
    tool_types = _build_tool_types(body)
    assert tool_types["apply_patch"] == "apply_patch"
    assert tool_types["web_search_preview"] == "web_search_preview"
    assert tool_types["local_shell"] == "local_shell"


def test_build_tool_types_mcp_tools():
    """MCP tools with function names are preserved."""
    body = {
        "tools": [
            {"type": "mcp__node_repl", "function": {"name": "js"}},
            {"type": "mcp__node_repl", "function": {"name": "eval"}},
        ]
    }
    tool_types = _build_tool_types(body)
    assert tool_types["js"] == "mcp__node_repl"
    assert tool_types["eval"] == "mcp__node_repl"


def test_build_tool_types_empty_and_missing():
    """Empty or missing tools arrays return empty dict."""
    assert _build_tool_types({}) == {}
    assert _build_tool_types({"tools": []}) == {}
    assert _build_tool_types({"tools": None}) == {}


def test_chat_completion_to_response_apply_patch_custom_tool_call():
    """apply_patch tool type maps to custom_tool_call output item."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "apply_patch", "arguments": '{"patch": "diff"}'},
                        }
                    ],
                }
            }
        ]
    }
    tool_types = {"apply_patch": "apply_patch"}
    response = chat_completion_to_response(payload, "model", tool_types)
    output = response["output"]
    call_items = [o for o in output if o["type"] in ("function_call", "custom_tool_call", "web_search_call")]
    assert len(call_items) == 1
    assert call_items[0]["type"] == "custom_tool_call"
    assert call_items[0]["name"] == "apply_patch"


def test_chat_completion_to_response_web_search_call():
    """web_search tool type maps to web_search_call output item."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "function": {"name": "web_search", "arguments": '{"query": "test"}'},
                        }
                    ],
                }
            }
        ]
    }
    tool_types = {"web_search": "web_search"}
    response = chat_completion_to_response(payload, "model", tool_types)
    output = response["output"]
    call_items = [o for o in output if o["type"] in ("function_call", "custom_tool_call", "web_search_call")]
    assert len(call_items) == 1
    assert call_items[0]["type"] == "web_search_call"
    assert call_items[0]["name"] == "web_search"


def test_chat_completion_to_response_unknown_tool_function_call():
    """Unknown tool types fall back to generic function_call."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_3",
                            "function": {"name": "random_tool", "arguments": '{"x": 1}'},
                        }
                    ],
                }
            }
        ]
    }
    tool_types = {"random_tool": "mcp__random"}
    response = chat_completion_to_response(payload, "model", tool_types)
    output = response["output"]
    call_items = [o for o in output if o["type"] in ("function_call", "custom_tool_call", "web_search_call")]
    assert len(call_items) == 1
    assert call_items[0]["type"] == "function_call"
    assert call_items[0]["name"] == "random_tool"


def test_anthropic_to_response_with_tool_types():
    """Anthropic path also maps apply_patch to custom_tool_call."""
    payload = {
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "tu_1", "name": "apply_patch", "input": {"patch": "diff"}},
        ],
        "id": "msg_1",
    }
    tool_types = {"apply_patch": "apply_patch"}
    response = anthropic_to_response(payload, "model", tool_types)
    output = response["output"]
    call_items = [o for o in output if o["type"] in ("function_call", "custom_tool_call", "web_search_call")]
    assert len(call_items) == 1
    assert call_items[0]["type"] == "custom_tool_call"


def test_chat_completion_to_response_no_tool_types_backward_compat():
    """Without tool_types, everything falls back to function_call (backward compat)."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "apply_patch", "arguments": '{"patch": "diff"}'},
                        }
                    ],
                }
            }
        ]
    }
    response = chat_completion_to_response(payload, "model")
    output = response["output"]
    call_items = [o for o in output if o["type"] in ("function_call", "custom_tool_call", "web_search_call")]
    assert len(call_items) == 1
    assert call_items[0]["type"] == "function_call"
