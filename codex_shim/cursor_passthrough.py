from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from typing import Any

from .translate import responses_to_chat, strip_think

CURSOR_MODEL_SLUG = "composer-2-5"
CURSOR_UPSTREAM_MODEL = "composer-2.5"
CURSOR_DISPLAY_NAME = "Composer 2.5"
CURSOR_PASSTHROUGH_SLUGS = frozenset({CURSOR_MODEL_SLUG, "composer-2.5"})
_AUTH_PROBE_TTL_SEC = 30.0
_auth_probe_cache: tuple[float, bool] | None = None


def cursor_spawn_env() -> dict[str, str]:
    """Environment for cursor-agent child processes.

    Following open-design's subscription-first pattern: a stale
    ``CURSOR_API_KEY`` in the shell must not override ``cursor-agent login``.
    """
    env = os.environ.copy()
    env.pop("CURSOR_API_KEY", None)
    bin_override = env.get("CURSOR_AGENT_BIN", "").strip()
    if bin_override:
        env["PATH"] = f"{os.path.dirname(bin_override)}:{env.get('PATH', '')}"
    return env


def _cursor_agent_bin() -> str:
    override = os.environ.get("CURSOR_AGENT_BIN", "").strip()
    if override:
        return override
    return shutil.which("cursor-agent") or "cursor-agent"


def _is_cursor_auth_failure(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "authentication required",
            "not authenticated",
            "not logged in",
            "please run",
            "agent login",
            "cursor_api_key",
        )
    )


def _probe_cursor_auth() -> bool:
    if os.environ.get("CODEX_SHIM_DISABLE_CURSOR", "").lower() in {"1", "true", "yes", "on"}:
        return False
    if not shutil.which(_cursor_agent_bin()) and not os.environ.get("CURSOR_AGENT_BIN"):
        return False
    try:
        result = subprocess.run(
            [_cursor_agent_bin(), "status"],
            capture_output=True,
            text=True,
            timeout=15,
            env=cursor_spawn_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{result.stdout}\n{result.stderr}"
    if _is_cursor_auth_failure(output):
        return False
    return "logged in" in output.lower()


def cursor_passthrough_available(*, force_refresh: bool = False) -> bool:
    """Return True when cursor-agent is installed and logged in."""
    global _auth_probe_cache
    now = time.monotonic()
    if not force_refresh and _auth_probe_cache is not None:
        cached_at, cached = _auth_probe_cache
        if now - cached_at < _AUTH_PROBE_TTL_SEC:
            return cached
    available = _probe_cursor_auth()
    _auth_probe_cache = (now, available)
    return available


def is_cursor_passthrough_slug(slug: str) -> bool:
    return slug in CURSOR_PASSTHROUGH_SLUGS


def cursor_upstream_model(_slug: str) -> str:
    return CURSOR_UPSTREAM_MODEL


def cursor_workspace() -> str:
    override = os.environ.get("CODEX_SHIM_CURSOR_WORKSPACE", "").strip()
    return override or os.getcwd()


def cursor_passthrough_display_names() -> dict[str, str]:
    return {CURSOR_MODEL_SLUG: CURSOR_DISPLAY_NAME}


def cursor_catalog_entry() -> dict[str, Any]:
    return {
        "slug": CURSOR_MODEL_SLUG,
        "display_name": CURSOR_DISPLAY_NAME,
        "description": "Cursor Composer 2.5 routed through your Cursor subscription (cursor-agent login).",
        "context_window": 272_000,
        "max_context_window": 272_000,
        "auto_compact_token_limit": 217_600,
        "truncation_policy": {"mode": "tokens", "limit": 64_000},
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": "low", "description": "Faster, lighter reasoning"},
            {"effort": "medium", "description": "Balanced speed and reasoning"},
            {"effort": "high", "description": "Deeper reasoning"},
        ],
        "default_reasoning_summary": "none",
        "reasoning_summary_format": "none",
        "supports_reasoning_summaries": False,
        "default_verbosity": "low",
        "support_verbosity": False,
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        "supports_search_tool": False,
        "supports_parallel_tool_calls": True,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
        "supports_image_detail_original": True,
        "shell_type": "shell_command",
        "visibility": "list",
        "minimal_client_version": "0.0.1",
        "supported_in_api": True,
        "availability_nux": None,
        "upgrade": None,
        "priority": 11000,
        "prefer_websockets": False,
        "available_in_plans": ["free", "plus", "pro", "team", "business", "enterprise"],
        "base_instructions": "You are Codex, a coding agent powered by Composer 2.5.",
        "model_messages": {
            "instructions_template": "You are Codex, a coding agent powered by Composer 2.5.",
            "instructions_variables": {"model_name": CURSOR_DISPLAY_NAME},
        },
    }


def build_cursor_prompt(body: dict[str, Any]) -> str:
    """Convert a Codex Responses payload into a cursor-agent prompt."""
    chat = responses_to_chat(body, cursor_upstream_model(str(body.get("model") or "")))
    sections: list[str] = []
    for message in chat.get("messages") or []:
        role = str(message.get("role") or "user").upper()
        content = _message_content(message)
        if not content:
            continue
        if role in {"SYSTEM", "DEVELOPER"}:
            sections.append(f"[{role}]\n{content}")
        elif role == "ASSISTANT":
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                rendered_calls = []
                for call in tool_calls:
                    fn = call.get("function") or {}
                    rendered_calls.append(
                        f"{fn.get('name') or 'tool'}({fn.get('arguments') or ''})"
                    )
                sections.append(f"[ASSISTANT]\n{content}\nTool calls: {', '.join(rendered_calls)}")
            else:
                sections.append(f"[ASSISTANT]\n{content}")
        elif role == "TOOL":
            sections.append(f"[TOOL {message.get('tool_call_id', '')}]\n{content}")
        else:
            sections.append(f"[USER]\n{content}")
    prompt = "\n\n".join(sections).strip()
    return prompt or "Continue."


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return strip_think(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in {"text", "input_text", "output_text"}:
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") in {"input_image", "image_url"}:
                    parts.append("[image omitted for cursor-agent bridge]")
        return strip_think("\n".join(part for part in parts if part))
    return ""


def _extract_cursor_assistant_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    ]
    return "".join(parts)


class CursorStreamParser:
    """Parse cursor-agent ``stream-json`` lines into text deltas and usage."""

    def __init__(self) -> None:
        self.text_so_far = ""
        self.final_text = ""
        self.usage: dict[str, Any] | None = None
        self.error: str | None = None

    def feed_line(self, line: str) -> str | None:
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None

        obj_type = obj.get("type")
        if obj_type == "assistant" and obj.get("message"):
            text = _extract_cursor_assistant_text(obj.get("message"))
            if not text:
                return None
            delta = self._delta_for_text(text)
            self.final_text = text
            return delta
        if obj_type == "result":
            if obj.get("subtype") == "error" or obj.get("is_error"):
                self.error = str(obj.get("result") or obj.get("error") or "cursor-agent failed")
            elif isinstance(obj.get("result"), str) and obj.get("result"):
                self.final_text = str(obj["result"])
            usage = obj.get("usage")
            if isinstance(usage, dict):
                self.usage = {
                    "input_tokens": usage.get("inputTokens"),
                    "output_tokens": usage.get("outputTokens"),
                    "cache_read_input_tokens": usage.get("cacheReadTokens"),
                    "cache_creation_input_tokens": usage.get("cacheWriteTokens"),
                }
            return None
        if obj_type == "error":
            self.error = str(obj.get("message") or obj.get("error") or "cursor-agent error")
        return None

    def _delta_for_text(self, text: str) -> str | None:
        if not self.text_so_far:
            self.text_so_far = text
            return text
        if text == self.text_so_far:
            return None
        if text.startswith(self.text_so_far):
            delta = text[len(self.text_so_far) :]
            self.text_so_far = text
            return delta or None
        self.text_so_far += text
        return text


async def iter_cursor_agent_events(prompt: str, model: str) -> AsyncIterator[dict[str, Any]]:
    """Spawn cursor-agent and yield normalized stream events."""
    cmd = [
        _cursor_agent_bin(),
        "--print",
        "--output-format",
        "stream-json",
        "--stream-partial-output",
        "--force",
        "--trust",
        "--workspace",
        cursor_workspace(),
        "--model",
        model,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=cursor_spawn_env(),
    )
    assert proc.stdout is not None
    stderr_chunks: list[bytes] = []

    async def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    stderr_task = asyncio.create_task(_drain_stderr())
    parser = CursorStreamParser()
    try:
        if proc.stdin is not None:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        buffer = ""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                delta = parser.feed_line(line)
                if delta:
                    yield {"type": "text_delta", "delta": delta}
        if buffer.strip():
            delta = parser.feed_line(buffer)
            if delta:
                yield {"type": "text_delta", "delta": delta}
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass

    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if parser.error:
        yield {"type": "error", "message": parser.error}
    elif proc.returncode not in (0, None) and not parser.final_text:
        message = stderr.strip() or f"cursor-agent exited with code {proc.returncode}"
        if _is_cursor_auth_failure(message):
            message = (
                "Cursor Agent is not authenticated. Run `cursor-agent login`, "
                "then `cursor-agent status`, and retry."
            )
        yield {"type": "error", "message": message}
    if parser.usage:
        yield {"type": "usage", "usage": parser.usage}
    if parser.final_text:
        yield {"type": "completed", "text": parser.final_text}
