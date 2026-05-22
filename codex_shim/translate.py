from __future__ import annotations

import json
import re
from typing import Any


THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)

_THINKING_MAGIC = "anthropic-thinking-v1:"


def _decode_thinking_blob(encoded: Any) -> dict[str, Any] | None:
    import base64

    if not isinstance(encoded, str) or not encoded.startswith(_THINKING_MAGIC):
        return None
    blob = encoded[len(_THINKING_MAGIC) :]
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def responses_to_chat(body: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    messages = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": _content_to_text(instructions)})
    # Chat-completions upstreams don't understand reasoning items; drop the
    # marker messages we emit for the Anthropic path.
    for m in _responses_input_to_messages(body.get("input")):
        if m.get("_reasoning_only"):
            continue
        messages.append(m)

    chat: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages or [{"role": "user", "content": ""}],
        "stream": bool(body.get("stream", False)),
    }
    _copy_if_present(body, chat, "temperature")
    _copy_if_present(body, chat, "top_p")
    _copy_if_present(body, chat, "max_output_tokens", "max_tokens")
    _copy_if_present(body, chat, "max_tokens")
    _copy_if_present(body, chat, "parallel_tool_calls")

    tools = _responses_tools_to_chat_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
        _copy_if_present(body, chat, "tool_choice")
    return chat


def responses_to_anthropic(body: dict[str, Any], upstream_model: str, max_tokens: int | None) -> dict[str, Any]:
    system_parts: list[str] = []
    instructions = body.get("instructions")
    if instructions:
        system_parts.append(_content_to_text(instructions))

    messages: list[dict[str, Any]] = []

    def append(role: str, content: Any) -> None:
        if messages and messages[-1]["role"] == role and isinstance(messages[-1]["content"], list) and isinstance(content, list):
            messages[-1]["content"].extend(content)
        else:
            messages.append({"role": role, "content": content})

    pending_thinking: list[dict[str, Any]] = []
    for chat_msg in _responses_input_to_messages(body.get("input")):
        role = chat_msg.get("role", "user")
        if chat_msg.get("_reasoning_only"):
            decoded = _decode_thinking_blob(chat_msg.get("encrypted_content"))
            if decoded is not None:
                pending_thinking.append(decoded)
            else:
                # Summary-only fallback: emit a plain `thinking` block (no
                # signature). Anthropic requires `signature` on the original
                # session; if we lack it, skip rather than upsetting strict
                # APIs.
                for summary in chat_msg.get("summary") or []:
                    text = summary.get("text") if isinstance(summary, dict) else None
                    if text:
                        pending_thinking.append({"type": "thinking", "thinking": text, "signature": ""})
            continue
        if role in {"system", "developer"}:
            system_parts.append(_content_to_text(chat_msg.get("content", "")))
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            blocks.extend(pending_thinking)
            pending_thinking = []
            text = chat_msg.get("content")
            if text:
                blocks.append({"type": "text", "text": _content_to_text(text)})
            for call in chat_msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                args_raw = fn.get("arguments") or ""
                try:
                    args_obj = json.loads(args_raw) if args_raw else {}
                except json.JSONDecodeError:
                    args_obj = {"_raw": args_raw}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or "call_0",
                        "name": fn.get("name") or "",
                        "input": args_obj,
                    }
                )
            if blocks:
                append("assistant", blocks)
            continue
        if role == "tool":
            # Reasoning items only attach to assistant turns; drop any pending
            # thinking when a tool result interrupts (shouldn't happen in
            # normal Codex flows but defensive).
            pending_thinking = []
            append(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": chat_msg.get("tool_call_id") or "call_0",
                        "content": _content_to_text(chat_msg.get("content", "")),
                    }
                ],
            )
            continue
        # user / anything else
        pending_thinking = []
        append(role, _content_to_text(chat_msg.get("content", "")))

    # If reasoning items appeared without a following assistant turn (e.g. the
    # final pending think after a tool_use round-trip), emit an assistant
    # message containing them so Anthropic's API accepts the followup.
    if pending_thinking:
        append("assistant", pending_thinking)

    anthropic: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages or [{"role": "user", "content": ""}],
        "max_tokens": int(body.get("max_output_tokens") or body.get("max_tokens") or max_tokens or 4096),
        "stream": bool(body.get("stream", False)),
    }
    if system_parts:
        anthropic["system"] = "\n\n".join(system_parts)
    _copy_if_present(body, anthropic, "temperature")
    _copy_if_present(body, anthropic, "top_p")

    tools = _responses_tools_to_anthropic_tools(body.get("tools"))
    if tools:
        anthropic["tools"] = tools
    return anthropic


def chat_to_responses_request(body: dict[str, Any], upstream_model: str, max_tokens: int | None = None) -> dict[str, Any]:
    converted = {
        "model": upstream_model,
        "input": body.get("messages", []),
        "stream": bool(body.get("stream", False)),
    }
    for src, dst in [("temperature", "temperature"), ("top_p", "top_p"), ("max_tokens", "max_output_tokens")]:
        if src in body:
            converted[dst] = body[src]
    if max_tokens and "max_output_tokens" not in converted:
        converted["max_output_tokens"] = max_tokens
    if "tools" in body:
        converted["tools"] = body["tools"]
    return converted


def chat_to_anthropic(body: dict[str, Any], upstream_model: str, max_tokens: int | None) -> dict[str, Any]:
    pseudo_responses = chat_to_responses_request(body, upstream_model, max_tokens=max_tokens)
    return responses_to_anthropic(pseudo_responses, upstream_model, max_tokens)


def anthropic_to_chat_response(payload: dict[str, Any], requested_model: str) -> dict[str, Any]:
    content = ""
    tool_calls = []
    for block in payload.get("content", []):
        if block.get("type") == "text":
            content += block.get("text", "")
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": _jsonish(block.get("input", {})),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": strip_think(content)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": payload.get("id", "chatcmpl-anthropic"),
        "object": "chat.completion",
        "created": 0,
        "model": requested_model,
        "choices": [{"index": 0, "message": message, "finish_reason": _anthropic_stop(payload.get("stop_reason"))}],
    }


def chat_completion_to_response(payload: dict[str, Any], requested_model: str) -> dict[str, Any]:
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output: list[dict[str, Any]] = []
    text = strip_think(message.get("content") or "")
    if text:
        output.append(
            {
                "id": "msg_0",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        output.append(
            {
                "id": call.get("id", "call_0"),
                "type": "function_call",
                "status": "completed",
                "call_id": call.get("id", "call_0"),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", ""),
            }
        )
    return {
        "id": payload.get("id", "resp_chat"),
        "object": "response",
        "created_at": payload.get("created", 0),
        "status": "completed",
        "model": requested_model,
        "output": output,
        "usage": payload.get("usage"),
    }


def anthropic_to_response(payload: dict[str, Any], requested_model: str) -> dict[str, Any]:
    return chat_completion_to_response(anthropic_to_chat_response(payload, requested_model), requested_model)


def strip_think(text: str) -> str:
    return THINK_RE.sub("", text or "")


def _responses_input_to_messages(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if not isinstance(value, list):
        return [{"role": "user", "content": _content_to_text(value)}]
    messages: list[dict[str, Any]] = []
    pending_tool_calls: list[dict[str, Any]] = []

    def flush_pending_assistant_tool_calls():
        if pending_tool_calls:
            messages.append({"role": "assistant", "content": None, "tool_calls": list(pending_tool_calls)})
            pending_tool_calls.clear()

    for item in value:
        if isinstance(item, str):
            flush_pending_assistant_tool_calls()
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"message", None} and "role" in item:
            flush_pending_assistant_tool_calls()
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            messages.append({"role": role, "content": _content_to_text(item.get("content", ""))})
        elif item_type in {"input_text", "text"}:
            flush_pending_assistant_tool_calls()
            messages.append({"role": "user", "content": _content_to_text(item)})
        elif item_type == "function_call":
            # Coalesce consecutive function_call items into a single assistant
            # message with multiple tool_calls so chat-completions upstreams
            # accept the subsequent tool messages.
            call_id = item.get("call_id") or item.get("id") or "call_0"
            pending_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "",
                    },
                }
            )
        elif item_type == "function_call_output":
            flush_pending_assistant_tool_calls()
            messages.append({"role": "tool", "tool_call_id": item.get("call_id"), "content": _content_to_text(item.get("output", ""))})
        elif item_type == "reasoning":
            # For Chat-Completions upstreams reasoning is informational only.
            # We keep it as a marker so the Anthropic translator can reattach
            # encrypted_content as a `thinking` block on the assistant turn.
            flush_pending_assistant_tool_calls()
            messages.append(
                {
                    "role": "assistant",
                    "_reasoning_only": True,
                    "encrypted_content": item.get("encrypted_content"),
                    "summary": item.get("summary") or [],
                    "content": None,
                }
            )
    flush_pending_assistant_tool_calls()
    return messages


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") in {"input_text", "output_text", "text"}:
                    parts.append(str(part.get("text", "")))
                elif "content" in part:
                    parts.append(_content_to_text(part["content"]))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text", ""))
        return str(content)
    return str(content)


def _responses_tools_to_chat_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            if "function" in tool:
                converted.append(tool)
            elif "name" in tool:
                converted.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.get("name"),
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters")
                            or {"type": "object", "properties": {}, "additionalProperties": True},
                        },
                    }
                )
        elif "name" in tool:
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters")
                        or {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
                    },
                }
            )
    return converted


def _responses_tools_to_anthropic_tools(tools: Any) -> list[dict[str, Any]]:
    chat_tools = _responses_tools_to_chat_tools(tools)
    converted = []
    for tool in chat_tools:
        fn = tool.get("function") or {}
        converted.append(
            {
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return [tool for tool in converted if tool.get("name")]


def _copy_if_present(src: dict[str, Any], dst: dict[str, Any], src_key: str, dst_key: str | None = None) -> None:
    if src_key in src and src[src_key] is not None:
        dst[dst_key or src_key] = src[src_key]


def _anthropic_stop(reason: Any) -> str:
    return "tool_calls" if reason == "tool_use" else "stop"


def _jsonish(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))
