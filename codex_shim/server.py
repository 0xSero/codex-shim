from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientSession, ClientTimeout, web

from .settings import DEFAULT_FACTORY_SETTINGS, DEFAULT_HOST, DEFAULT_PORT, FactoryModel, FactorySettings
from .translate import (
    anthropic_to_chat_response,
    anthropic_to_response,
    chat_completion_to_response,
    chat_to_anthropic,
    responses_to_anthropic,
    responses_to_chat,
)


class ShimServer:
    def __init__(self, settings_path: Path = DEFAULT_FACTORY_SETTINGS):
        self.settings = FactorySettings(settings_path)
        self.timeout = ClientTimeout(total=None, sock_connect=120, sock_read=None)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_get("/v1/models", self.models)
        app.router.add_post("/v1/chat/completions", self.chat_completions)
        app.router.add_post("/v1/responses", self.responses)
        return app

    async def health(self, _request: web.Request) -> web.Response:
        models = self.settings.load()
        return web.json_response({"ok": True, "models": len(models)})

    async def models(self, _request: web.Request) -> web.Response:
        now = int(time.time())
        data = [{"id": model.slug, "object": "model", "created": now, "owned_by": "factory"} for model in self.settings.load()]
        return web.json_response({"object": "list", "data": data})

    async def chat_completions(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        route = self._route(body)
        if route.is_openai_chat:
            forwarded = dict(body)
            forwarded["model"] = route.model
            return await self._post_openai_chat(request, route, forwarded, as_responses=False)
        if route.is_anthropic:
            forwarded = chat_to_anthropic(body, route.model, route.max_output_tokens)
            return await self._post_anthropic(request, route, forwarded, as_responses=False)
        raise web.HTTPBadGateway(text=f"Unsupported Factory provider: {route.provider}")

    async def responses(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        _log_incoming_request("/v1/responses", body)
        model = str(body.get("model") or "")
        if model == "gpt-5.5" or model.startswith("openai-gpt-5-5"):
            return await self._chatgpt_passthrough(request, body)
        # Auto-forward only the current image generation request to ChatGPT.
        # Rewrite response model metadata back to the originally selected model so
        # Codex does not switch the rest of the conversation to GPT-5.5.
        if self._needs_image_gen(body):
            return await self._chatgpt_passthrough(request, body, response_model_override=model)
        # If the conversation already contains a ChatGPT-generated image and
        # the latest user turn asks to inspect/edit that image, keep that turn
        # on ChatGPT too. The custom model usually receives only the
        # image_generation_call reference, not the image pixels/state.
        if self._needs_image_followup(body):
            return await self._chatgpt_passthrough(request, body, response_model_override=model)
        route = self._route(body)
        if route.is_openai_chat:
            forwarded = responses_to_chat(body, route.model)
            return await self._post_openai_chat(request, route, forwarded, as_responses=True)
        if route.is_anthropic:
            forwarded = responses_to_anthropic(body, route.model, route.max_output_tokens)
            return await self._post_anthropic(request, route, forwarded, as_responses=True)
        raise web.HTTPBadGateway(text=f"Unsupported Factory provider: {route.provider}")

    def _needs_image_gen(self, body: dict[str, Any]) -> bool:
        """Detect if this request is explicitly an image generation turn.

        Codex Desktop may include the image_generation tool in the *available*
        tool list for normal chat requests. Do not treat mere availability as a
        reason to route to ChatGPT, or normal custom-model turns get hijacked.

        Route to ChatGPT when image generation is explicitly selected via
        tool_choice, when the request's tool list is image-generation only, or
        when the latest user turn clearly asks for image/imagegen output.
        """
        tools = body.get("tools") or []
        image_tool_names: set[str] = set()
        non_image_tool_count = 0
        for tool in tools:
            if not isinstance(tool, dict):
                non_image_tool_count += 1
                continue
            tool_type = str(tool.get("type") or "")
            fn = tool.get("function") or tool.get("name") or {}
            name = fn.get("name") if isinstance(fn, dict) else fn
            normalized = f"{tool_type} {name or ''}".lower()
            is_image_tool = tool_type in ("image_generation", "image_gen") or (
                "image" in normalized and "gen" in normalized
            )
            if is_image_tool:
                image_tool_names.add(str(name or tool_type))
            else:
                non_image_tool_count += 1

        if not image_tool_names:
            return False

        tool_choice = body.get("tool_choice")
        if isinstance(tool_choice, str):
            if any(name.lower() in tool_choice.lower() for name in image_tool_names):
                return True
        elif isinstance(tool_choice, dict):
            choice_name = str(
                tool_choice.get("name")
                or (tool_choice.get("function") or {}).get("name")
                or tool_choice.get("type")
                or ""
            ).lower()
            if any(name.lower() in choice_name for name in image_tool_names):
                return True

        # Direct imagegen requests observed from Codex use only the
        # image_generation tool. Normal chat requests include many other tools.
        if non_image_tool_count == 0:
            return True

        # The imagegen skill can start as a normal model turn with many tools
        # available. In that case, infer intent from only the latest user text
        # so old history does not keep the thread on ChatGPT.
        latest = self._latest_user_text(body).lower()
        if not latest:
            return False
        image_intent_markers = (
            "@image",
            "imagegen",
            "image gen",
            "image_gen",
            "generate image",
            "generate an image",
            "generate a picture",
            "generate a photo",
            "generate an illustration",
            "create image",
            "create an image",
            "create a picture",
            "create a photo",
            "draw image",
            "draw an image",
            "make image",
            "make an image",
            "render image",
        )
        if any(marker in latest for marker in image_intent_markers):
            return True

        # Heuristic for common asset requests, but avoid hijacking coding tasks
        # like "create a React icon component" or "generate SVG".
        code_words = {"code", "component", "react", "tsx", "jsx", "html", "css", "svg", "file"}
        latest_words = {"".join(ch for ch in word if ch.isalnum()) for word in latest.split()}
        if latest_words & code_words:
            return False
        creative_objects = ("icon", "logo", "wallpaper", "poster", "banner", "avatar")
        creative_verbs = ("generate", "create", "draw", "design", "make", "render")
        return any(verb in latest for verb in creative_verbs) and any(obj in latest for obj in creative_objects)

    def _needs_image_followup(self, body: dict[str, Any]) -> bool:
        if not self._has_image_generation_history(body):
            return False
        latest = self._latest_user_text(body).lower()
        if not latest:
            return False
        direct_image_refs = ("image", "picture", "photo", "icon", "logo", "illustration")
        followup_actions = (
            "inspect",
            "look at",
            "view",
            "describe",
            "what do you see",
            "analyze",
            "modify",
            "edit",
            "change",
            "improve",
            "enhance",
            "upscale",
            "variation",
            "use",
            "based on",
            "same",
        )
        if any(ref in latest for ref in direct_image_refs) and any(action in latest for action in followup_actions):
            return True
        pronoun_followups = (
            "inspect it",
            "look at it",
            "view it",
            "describe it",
            "analyze it",
            "modify it",
            "edit it",
            "change it",
            "improve it",
            "enhance it",
            "upscale it",
            "make it brighter",
            "make it darker",
            "make it more",
            "use it",
            "based on it",
        )
        return any(marker in latest for marker in pronoun_followups)

    def _has_image_generation_history(self, body: dict[str, Any]) -> bool:
        inputs = body.get("input") or []
        if not isinstance(inputs, list):
            return False
        return any(isinstance(item, dict) and item.get("type") == "image_generation_call" for item in inputs)

    def _latest_user_text(self, body: dict[str, Any]) -> str:
        inputs = body.get("input") or []
        if not isinstance(inputs, list):
            return ""
        for item in reversed(inputs):
            if not isinstance(item, dict):
                continue
            if item.get("role") == "user":
                text = self._content_to_debug_text(item.get("content"))
                if text:
                    return text
            elif item.get("type") in {"input_text", "text"}:
                text = self._content_to_debug_text(item)
                if text:
                    return text
        return ""

    def _content_to_debug_text(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get("text") or part.get("content") or ""))
                else:
                    parts.append(str(part))
            return "\n".join(p for p in parts if p)
        if isinstance(content, dict):
            return str(content.get("text") or content.get("content") or "")
        return str(content)

    async def _chatgpt_passthrough(
        self, request: web.Request, body: dict[str, Any], response_model_override: str | None = None
    ) -> web.StreamResponse:
        """Forward a Responses request to chatgpt.com using the user's Codex auth.

        Lets the picker expose OpenAI's real GPT-5.5 (ChatGPT subscription) as a
        first-class model alongside Factory BYOK entries.
        """
        auth_path = Path("~/.codex/auth.json").expanduser()
        try:
            auth = json.loads(auth_path.read_text())
        except FileNotFoundError:
            raise web.HTTPUnauthorized(text="~/.codex/auth.json not found")
        tokens = auth.get("tokens") or {}
        access_token = tokens.get("access_token")
        account_id = tokens.get("account_id") or ""
        if not access_token:
            raise web.HTTPUnauthorized(text="auth.json has no access_token")
        forwarded = _sanitize_chatgpt_passthrough_body(body) if response_model_override else dict(body)
        forwarded["model"] = "gpt-5.5"
        forwarded["store"] = False
        forwarded["stream"] = True
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if forwarded.get("stream") else "application/json",
            "OpenAI-Beta": "responses=2026-02-06",
            "originator": "codex_cli_rs",
            "chatgpt-account-id": account_id,
            "session_id": request.headers.get("session_id", ""),
        }
        url = "https://chatgpt.com/backend-api/codex/responses"
        async with ClientSession(timeout=self.timeout) as session:
            upstream = await session.post(url, json=forwarded, headers=headers)
            if upstream.status >= 400:
                return await _error_response(upstream)
            if not forwarded.get("stream"):
                payload = await upstream.json(content_type=None)
                _rewrite_response_model(payload, response_model_override)
                return web.json_response(payload)
            response = _sse_response()
            await response.prepare(request)
            try:
                if response_model_override:
                    async for line in _sse_lines(upstream):
                        if line == "[DONE]":
                            await _safe_write(response, b"data: [DONE]\n\n")
                            break
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            await _safe_write(response, f"data: {line}\n\n".encode())
                            continue
                        _rewrite_response_model(payload, response_model_override)
                        await _write_sse(response, payload)
                else:
                    async for chunk in upstream.content.iter_chunked(4096):
                        await _safe_write(response, chunk)
            except ClientDisconnected:
                pass
            finally:
                upstream.release()
            try:
                await response.write_eof()
            except Exception:
                pass
            return response

    def _route(self, body: dict[str, Any]) -> FactoryModel:
        requested = str(body.get("model") or "")
        route = self.settings.by_slug_or_model(requested)
        if route is None:
            raise web.HTTPNotFound(text=f"Unknown model slug/model: {requested}")
        return route

    async def _post_openai_chat(
        self, request: web.Request, route: FactoryModel, body: dict[str, Any], as_responses: bool
    ) -> web.StreamResponse:
        url = _join_url(route.base_url, "/chat/completions")
        headers = _openai_headers(route)
        async with ClientSession(timeout=self.timeout) as session:
            upstream = await session.post(url, json=body, headers=headers)
            if upstream.status >= 400:
                return await _error_response(upstream)
            if body.get("stream"):
                return await self._stream_openai_chat(request, upstream, route, as_responses)
            payload = await upstream.json(content_type=None)
        if as_responses:
            return web.json_response(chat_completion_to_response(payload, route.slug))
        return web.json_response(payload)

    async def _post_anthropic(
        self, request: web.Request, route: FactoryModel, body: dict[str, Any], as_responses: bool
    ) -> web.StreamResponse:
        url = _join_url(route.base_url, "/messages")
        headers = _anthropic_headers(route)
        async with ClientSession(timeout=self.timeout) as session:
            upstream = await session.post(url, json=body, headers=headers)
            if upstream.status >= 400:
                return await _error_response(upstream)
            if body.get("stream"):
                return await self._stream_anthropic(request, upstream, route, as_responses)
            payload = await upstream.json(content_type=None)
        if as_responses:
            return web.json_response(anthropic_to_response(payload, route.slug))
        return web.json_response(anthropic_to_chat_response(payload, route.slug))

    async def _stream_openai_chat(
        self, request: web.Request, upstream, route: FactoryModel, as_responses: bool
    ) -> web.StreamResponse:
        response = _sse_response()
        await response.prepare(request)
        if as_responses:
            state = ResponsesStreamState(route.slug)
        try:
            if as_responses:
                await state.start(response)
            async for line in _sse_lines(upstream):
                if line == "[DONE]":
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if as_responses:
                    await state.write_chat_delta(response, event)
                else:
                    await _write_sse(response, event)
            if as_responses:
                await state.finish(response)
            else:
                await _safe_write(response, b"data: [DONE]\n\n")
        except ClientDisconnected:
            pass
        finally:
            upstream.release()
        try:
            await response.write_eof()
        except Exception:
            pass
        return response

    async def _stream_anthropic(
        self, request: web.Request, upstream, route: FactoryModel, as_responses: bool
    ) -> web.StreamResponse:
        response = _sse_response()
        await response.prepare(request)
        if as_responses:
            state = ResponsesStreamState(route.slug)
        try:
            if as_responses:
                await state.start(response)
            async for line in _sse_lines(upstream):
                if line == "[DONE]":
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if as_responses:
                    await state.write_anthropic_delta(response, event)
                else:
                    await _write_sse(response, _anthropic_stream_to_chat_chunk(event, route.slug))
            if as_responses:
                await state.finish(response)
            else:
                await _safe_write(response, b"data: [DONE]\n\n")
        except ClientDisconnected:
            pass
        finally:
            upstream.release()
        try:
            await response.write_eof()
        except Exception:
            pass
        return response


class ResponsesStreamState:
    """Translates upstream chat-completions / anthropic stream events into the
    Codex Desktop Responses-API event sequence. Keeps the message item and
    each tool call as separate output items with stable indices, and emits
    proper .added / .delta / .done / .completed events plus a final
    `response.completed` with the full reconciled `output` array."""

    def __init__(self, model: str):
        self.response_id = f"resp_{int(time.time() * 1000)}"
        self.message_item_id = f"msg_{int(time.time() * 1000)}"
        self.model = model
        self.message_index: int | None = None  # output_index for the assistant message
        self.message_text = ""
        self.message_opened = False
        self.message_closed = False
        # Tool call state, keyed by upstream "index" (chat-completions) or
        # anthropic content_block_index. Each entry tracks its assigned
        # output_index, accumulated arguments, name, etc.
        self.tool_calls: dict[int, dict[str, Any]] = {}
        # Reasoning (extended thinking) blocks, keyed by upstream index.
        self.reasoning_blocks: dict[Any, dict[str, Any]] = {}
        self.next_output_index = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self, response: web.StreamResponse) -> None:
        await _write_sse(response, {"type": "response.created", "response": self._response("in_progress")})

    async def finish(self, response: web.StreamResponse) -> None:
        if self.message_opened and not self.message_closed:
            await self._close_message(response)
        for state in sorted(self.tool_calls.values(), key=lambda s: s["output_index"]):
            if not state.get("closed"):
                await self._close_tool(response, state)
        for state in sorted(self.reasoning_blocks.values(), key=lambda s: s["output_index"]):
            if not state.get("closed"):
                await self._close_reasoning(response, state)
        await _write_sse(response, {"type": "response.completed", "response": self._response("completed", final=True)})
        await response.write(b"data: [DONE]\n\n")

    # ------------------------------------------------------------------
    # Chat-completions (OpenAI-style) deltas
    # ------------------------------------------------------------------
    async def write_chat_delta(self, response: web.StreamResponse, chunk: dict[str, Any]) -> None:
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            await self._chat_reasoning_delta(response, reasoning)
        content = delta.get("content")
        if content:
            await self._text_delta(response, content)
        for call in delta.get("tool_calls") or []:
            await self._chat_tool_delta(response, call)

    async def _chat_reasoning_delta(self, response: web.StreamResponse, text: str) -> None:
        state = self.reasoning_blocks.get(("chat",))
        if state is None:
            state = await self._open_reasoning(response, key=("chat",))
        state["text"] += text
        await _write_sse(
            response,
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": state["id"],
                "output_index": state["output_index"],
                "summary_index": 0,
                "delta": text,
            },
        )

    async def _chat_tool_delta(self, response: web.StreamResponse, call: dict[str, Any]) -> None:
        index = int(call.get("index", 0))
        fn = call.get("function") or {}
        state = self.tool_calls.get(index)
        if state is None:
            call_id = call.get("id") or f"call_{index}"
            state = await self._open_tool(response, key=index, call_id=call_id, name=fn.get("name") or "")
        else:
            if fn.get("name"):
                state["name"] += fn["name"]
        arg_delta = fn.get("arguments") or ""
        if arg_delta:
            state["arguments"] += arg_delta
            await _write_sse(
                response,
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": state["id"],
                    "output_index": state["output_index"],
                    "delta": arg_delta,
                },
            )

    # ------------------------------------------------------------------
    # Anthropic deltas
    # ------------------------------------------------------------------
    async def write_anthropic_delta(self, response: web.StreamResponse, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "content_block_start":
            block = event.get("content_block") or {}
            idx = int(event.get("index", 0))
            btype = block.get("type")
            if btype == "text":
                seed = block.get("text") or ""
                if seed:
                    await self._text_delta(response, seed)
            elif btype == "tool_use":
                await self._open_tool(
                    response,
                    key=("anthropic", idx),
                    call_id=block.get("id") or f"call_{idx}",
                    name=block.get("name") or "",
                )
            elif btype in {"thinking", "redacted_thinking"}:
                await self._open_reasoning(
                    response,
                    key=("anthropic_thinking", idx),
                    initial_text=block.get("thinking") or "",
                    initial_signature=block.get("signature") or "",
                    redacted=(btype == "redacted_thinking"),
                    redacted_data=block.get("data") or "",
                )
        elif event_type == "content_block_delta":
            idx = int(event.get("index", 0))
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                await self._text_delta(response, delta.get("text", ""))
            elif dtype == "input_json_delta":
                state = self.tool_calls.get(("anthropic", idx))
                if state is not None:
                    arg_delta = delta.get("partial_json") or ""
                    if arg_delta:
                        state["arguments"] += arg_delta
                        await _write_sse(
                            response,
                            {
                                "type": "response.function_call_arguments.delta",
                                "item_id": state["id"],
                                "output_index": state["output_index"],
                                "delta": arg_delta,
                            },
                        )
            elif dtype == "thinking_delta":
                state = self.reasoning_blocks.get(("anthropic_thinking", idx))
                if state is None:
                    state = await self._open_reasoning(response, key=("anthropic_thinking", idx))
                txt = delta.get("thinking") or ""
                if txt:
                    state["text"] += txt
                    await _write_sse(
                        response,
                        {
                            "type": "response.reasoning_summary_text.delta",
                            "item_id": state["id"],
                            "output_index": state["output_index"],
                            "summary_index": 0,
                            "delta": txt,
                        },
                    )
            elif dtype == "signature_delta":
                state = self.reasoning_blocks.get(("anthropic_thinking", idx))
                if state is None:
                    state = await self._open_reasoning(response, key=("anthropic_thinking", idx))
                state["signature"] += delta.get("signature") or ""
        elif event_type == "content_block_stop":
            idx = int(event.get("index", 0))
            tool_state = self.tool_calls.get(("anthropic", idx))
            if tool_state is not None and not tool_state.get("closed"):
                await self._close_tool(response, tool_state)
            r_state = self.reasoning_blocks.get(("anthropic_thinking", idx))
            if r_state is not None and not r_state.get("closed"):
                await self._close_reasoning(response, r_state)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _open_message(self, response: web.StreamResponse) -> None:
        self.message_index = self.next_output_index
        self.next_output_index += 1
        self.message_opened = True
        await _write_sse(
            response,
            {
                "type": "response.output_item.added",
                "output_index": self.message_index,
                "item": {
                    "id": self.message_item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            },
        )
        await _write_sse(
            response,
            {
                "type": "response.content_part.added",
                "item_id": self.message_item_id,
                "output_index": self.message_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        )

    async def _close_message(self, response: web.StreamResponse) -> None:
        if not self.message_opened or self.message_closed:
            return
        self.message_closed = True
        await _write_sse(
            response,
            {
                "type": "response.output_text.done",
                "item_id": self.message_item_id,
                "output_index": self.message_index,
                "content_index": 0,
                "text": self.message_text,
            },
        )
        await _write_sse(
            response,
            {
                "type": "response.content_part.done",
                "item_id": self.message_item_id,
                "output_index": self.message_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": self.message_text, "annotations": []},
            },
        )
        await _write_sse(
            response,
            {
                "type": "response.output_item.done",
                "output_index": self.message_index,
                "item": self._message_item("completed"),
            },
        )

    async def _text_delta(self, response: web.StreamResponse, text: str) -> None:
        if not text:
            return
        if not self.message_opened:
            await self._open_message(response)
        self.message_text += text
        await _write_sse(
            response,
            {
                "type": "response.output_text.delta",
                "item_id": self.message_item_id,
                "output_index": self.message_index,
                "content_index": 0,
                "delta": text,
            },
        )

    async def _open_tool(self, response: web.StreamResponse, *, key: Any, call_id: str, name: str) -> dict[str, Any]:
        # Close the assistant message before opening tool items, matching the
        # OpenAI Responses-API ordering Codex expects.
        if self.message_opened and not self.message_closed:
            await self._close_message(response)
        output_index = self.next_output_index
        self.next_output_index += 1
        state: dict[str, Any] = {
            "id": call_id,
            "call_id": call_id,
            "name": name,
            "arguments": "",
            "output_index": output_index,
            "closed": False,
        }
        self.tool_calls[key] = state
        await _write_sse(
            response,
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {
                    "id": call_id,
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": call_id,
                    "name": name,
                    "arguments": "",
                },
            },
        )
        return state

    async def _close_tool(self, response: web.StreamResponse, state: dict[str, Any]) -> None:
        state["closed"] = True
        await _write_sse(
            response,
            {
                "type": "response.function_call_arguments.done",
                "item_id": state["id"],
                "output_index": state["output_index"],
                "arguments": state["arguments"],
            },
        )
        await _write_sse(
            response,
            {
                "type": "response.output_item.done",
                "output_index": state["output_index"],
                "item": self._tool_item(state, "completed"),
            },
        )

    async def _open_reasoning(
        self,
        response: web.StreamResponse,
        *,
        key: Any,
        initial_text: str = "",
        initial_signature: str = "",
        redacted: bool = False,
        redacted_data: str = "",
    ) -> dict[str, Any]:
        # Reasoning items are emitted before the assistant message/tool calls
        # so we open them eagerly. If a message/tool was already opened we
        # still slot them in at the next available output_index; Codex orders
        # by output_index when reconciling.
        output_index = self.next_output_index
        self.next_output_index += 1
        item_id = f"rs_{int(time.time() * 1000)}_{output_index}"
        state: dict[str, Any] = {
            "id": item_id,
            "output_index": output_index,
            "text": initial_text,
            "signature": initial_signature,
            "redacted": redacted,
            "redacted_data": redacted_data,
            "closed": False,
        }
        self.reasoning_blocks[key] = state
        await _write_sse(
            response,
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {
                    "id": item_id,
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": [],
                    "encrypted_content": None,
                },
            },
        )
        if initial_text:
            await _write_sse(
                response,
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": item_id,
                    "output_index": output_index,
                    "summary_index": 0,
                    "delta": initial_text,
                },
            )
        return state

    async def _close_reasoning(self, response: web.StreamResponse, state: dict[str, Any]) -> None:
        state["closed"] = True
        # Emit summary_text.done so renderers can finalize the reasoning bubble.
        await _write_sse(
            response,
            {
                "type": "response.reasoning_summary_text.done",
                "item_id": state["id"],
                "output_index": state["output_index"],
                "summary_index": 0,
                "text": state["text"],
            },
        )
        await _write_sse(
            response,
            {
                "type": "response.output_item.done",
                "output_index": state["output_index"],
                "item": self._reasoning_item(state, "completed"),
            },
        )

    def _reasoning_item(self, state: dict[str, Any], status: str) -> dict[str, Any]:
        # Encode the original Anthropic thinking block in encrypted_content so
        # we can roundtrip it back on the next turn. Codex preserves this
        # field verbatim across turns.
        if state.get("redacted"):
            payload = {"type": "redacted_thinking", "data": state.get("redacted_data", "")}
        else:
            payload = {
                "type": "thinking",
                "thinking": state.get("text", ""),
                "signature": state.get("signature", ""),
            }
        encrypted = _encode_thinking_payload(payload)
        return {
            "id": state["id"],
            "type": "reasoning",
            "status": status,
            "summary": (
                [{"type": "summary_text", "text": state.get("text", "")}]
                if state.get("text") and not state.get("redacted")
                else []
            ),
            "encrypted_content": encrypted,
        }

    def _message_item(self, status: str) -> dict[str, Any]:
        return {
            "id": self.message_item_id,
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": self.message_text, "annotations": []}
            ] if self.message_text else [],
        }

    def _tool_item(self, state: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            "id": state["id"],
            "type": "function_call",
            "status": status,
            "call_id": state["call_id"],
            "name": state["name"],
            "arguments": state["arguments"],
        }

    def _response(self, status: str, *, final: bool = False) -> dict[str, Any]:
        output: list[dict[str, Any]] = []
        if final:
            collected: list[tuple[int, dict[str, Any]]] = []
            for state in self.reasoning_blocks.values():
                collected.append((state["output_index"], self._reasoning_item(state, "completed")))
            if self.message_opened and self.message_text and self.message_index is not None:
                collected.append((self.message_index, self._message_item("completed")))
            for state in self.tool_calls.values():
                collected.append((state["output_index"], self._tool_item(state, "completed")))
            collected.sort(key=lambda pair: pair[0])
            output = [item for _, item in collected]
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": self.model,
            "output": output,
        }


_THINKING_MAGIC = "anthropic-thinking-v1:"


def _encode_thinking_payload(payload: dict[str, Any]) -> str:
    import base64

    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _THINKING_MAGIC + base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_thinking_payload(encoded: str) -> dict[str, Any] | None:
    import base64

    if not isinstance(encoded, str) or not encoded.startswith(_THINKING_MAGIC):
        return None
    blob = encoded[len(_THINKING_MAGIC) :]
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _join_url(base_url: str, endpoint: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base + endpoint
    if endpoint == "/messages":
        return base + "/v1/messages"
    return urljoin(base + "/", "v1" + endpoint)


def _openai_headers(route: FactoryModel) -> dict[str, str]:
    headers = {"Content-Type": "application/json", **route.extra_headers}
    if route.api_key:
        headers.setdefault("Authorization", f"Bearer {route.api_key}")
    return headers


def _anthropic_headers(route: FactoryModel) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        **route.extra_headers,
    }
    if route.api_key:
        headers.setdefault("x-api-key", route.api_key)
        headers.setdefault("Authorization", f"Bearer {route.api_key}")
    return headers


def _sanitize_chatgpt_passthrough_body(body: dict[str, Any]) -> dict[str, Any]:
    """Remove foreign encrypted reasoning before hybrid ChatGPT passthrough.

    Custom Anthropic-shaped models can round-trip reasoning as synthetic
    encrypted_content values prefixed with anthropic-thinking-v1:. ChatGPT's
    backend cannot decrypt/verify those blobs, so image-gen passthrough must
    drop them while preserving the rest of the conversation.
    """
    sanitized = json.loads(json.dumps(body))
    inputs = sanitized.get("input")
    if isinstance(inputs, list):
        sanitized["input"] = [item for item in inputs if not _is_foreign_reasoning_item(item)]
    _strip_foreign_encrypted_content(sanitized)
    return sanitized


def _is_foreign_reasoning_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    encrypted = item.get("encrypted_content")
    return item.get("type") == "reasoning" and isinstance(encrypted, str) and encrypted.startswith("anthropic-thinking-v1:")


def _strip_foreign_encrypted_content(value: Any) -> None:
    if isinstance(value, dict):
        encrypted = value.get("encrypted_content")
        if isinstance(encrypted, str) and encrypted.startswith("anthropic-thinking-v1:"):
            value.pop("encrypted_content", None)
        for child in value.values():
            _strip_foreign_encrypted_content(child)
    elif isinstance(value, list):
        for child in value:
            _strip_foreign_encrypted_content(child)


def _rewrite_response_model(payload: Any, model: str | None) -> None:
    """Rewrite ChatGPT passthrough response metadata to the caller's model.

    Image generation is executed by ChatGPT, whose response events contain
    model='gpt-5.5'. If that metadata is returned unchanged, Codex may switch
    subsequent turns in the same conversation to GPT-5.5. For hybrid image-gen
    passthrough, keep the response associated with the originally selected
    shim model instead.
    """
    if not model:
        return
    if isinstance(payload, dict):
        if payload.get("model") == "gpt-5.5":
            payload["model"] = model
        for value in payload.values():
            _rewrite_response_model(value, model)
    elif isinstance(payload, list):
        for item in payload:
            _rewrite_response_model(item, model)


def _sse_response() -> web.StreamResponse:
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    return response


async def _safe_write(response: web.StreamResponse, data: bytes) -> None:
    try:
        await response.write(data)
    except (ConnectionResetError, ConnectionError):
        raise ClientDisconnected()
    except Exception as exc:
        if exc.__class__.__name__ in {
            "ClientConnectionResetError",
            "ClientConnectionError",
            "ClientPayloadError",
        }:
            raise ClientDisconnected() from exc
        raise


async def _write_sse(response: web.StreamResponse, payload: dict[str, Any]) -> None:
    try:
        await response.write(f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode())
    except (ConnectionResetError, ConnectionError) as exc:
        raise ClientDisconnected() from exc
    except Exception as exc:
        # aiohttp raises ClientConnectionResetError (an OSError subclass on
        # some versions, a ClientConnectionError on others). Trap both.
        if exc.__class__.__name__ in {
            "ClientConnectionResetError",
            "ClientConnectionError",
            "ClientPayloadError",
        }:
            raise ClientDisconnected() from exc
        raise


class ClientDisconnected(Exception):
    """Raised when the downstream Codex client closes the SSE connection."""


def _log_incoming_request(endpoint: str, body: dict[str, Any]) -> None:
    try:
        tools = body.get("tools") or []
        names = []
        for t in tools[:80]:
            if isinstance(t, dict):
                name = t.get("name") or (t.get("function") or {}).get("name") or t.get("type")
                if name:
                    names.append(str(name))
        input_items = body.get("input") or []
        input_summary = []
        if isinstance(input_items, list):
            for item in input_items[-6:]:
                if isinstance(item, dict):
                    t = item.get("type") or item.get("role") or "?"
                    extra = ""
                    if t == "function_call":
                        extra = f"({item.get('name', '?')})"
                    elif t == "function_call_output":
                        extra = f"(call_id={str(item.get('call_id', ''))[:24]})"
                    input_summary.append(f"{t}{extra}")
        print(
            f"[req] {endpoint} model={body.get('model')!r} stream={body.get('stream')!r} "
            f"tools={len(tools)} ({names[:8]}) "
            f"input={len(input_items)} ({input_summary})",
            flush=True,
        )
    except Exception as exc:
        print(f"[req] failed to log: {exc}", flush=True)


async def _sse_lines(upstream) -> Any:
    buffer = b""
    async for chunk in upstream.content.iter_chunked(4096):
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("data:"):
                yield line[5:].strip()
    tail = buffer.decode("utf-8", errors="replace").strip()
    if tail.startswith("data:"):
        yield tail[5:].strip()


def _anthropic_stream_to_chat_chunk(event: dict[str, Any], model: str) -> dict[str, Any]:
    content = ""
    if event.get("type") == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            content = delta.get("text", "")
    return {"object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}


async def _error_response(upstream) -> web.Response:
    text = await upstream.text()
    return web.Response(status=upstream.status, text=text, content_type=upstream.content_type or "text/plain")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, default=DEFAULT_FACTORY_SETTINGS)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    shim = ShimServer(args.settings)
    web.run_app(shim.app(), host=args.host, port=args.port, handle_signals=True)


if __name__ == "__main__":
    main()
