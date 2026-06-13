from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientSession, ClientTimeout, web

from .cursor_passthrough import (
    CURSOR_MODEL_SLUG,
    build_cursor_prompt,
    cursor_passthrough_available,
    cursor_passthrough_display_names,
    cursor_upstream_model,
    is_cursor_passthrough_slug,
    iter_cursor_agent_events,
)
from . import router as router_module
from .hostguard import build_allowed_hosts, host_guard_middleware
from .settings import (
    CHATGPT_MODEL_SLUG,
    DEFAULT_CODEX_AUTH,
    DEFAULT_SETTINGS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    PROVIDER_NAME,
    ModelSettings,
    ShimModel,
    available_model_slugs,
    chatgpt_passthrough_available,
    chatgpt_passthrough_display_names,
    chatgpt_passthrough_slugs,
    byok_model_has_credentials,
    chatgpt_upstream_model,
    is_chatgpt_passthrough_slug,
    usable_byok_models,
)
from .translate import (
    SHIM_ENCRYPTED_CONTENT_PREFIX,
    anthropic_messages_to_chat,
    anthropic_to_chat_response,
    anthropic_to_response,
    chat_completion_to_anthropic_message,
    chat_completion_to_response,
    chat_to_anthropic,
    normalize_responses_usage,
    responses_to_anthropic,
    responses_to_chat,
    _chat_finish_to_anthropic_stop,
    _responses_usage_to_anthropic_usage,
)

DEBUG_DIR = Path(__file__).resolve().parents[1] / ".codex-shim"
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
PICKER_TOKEN_HEADER = "X-Codex-Shim-Picker-Token"


class ShimServer:
    def __init__(self, settings_path: Path = DEFAULT_SETTINGS, host: str = DEFAULT_HOST):
        self.settings = ModelSettings(settings_path)
        self.host = host
        self.timeout = ClientTimeout(total=None, sock_connect=120, sock_read=None)
        self.picker_token = secrets.token_urlsafe(32)

    def app(self) -> web.Application:
        allowed_hosts = build_allowed_hosts(self.host)
        app = web.Application(
            client_max_size=64 * 1024 * 1024,
            middlewares=[host_guard_middleware(allowed_hosts)],
        )
        app.router.add_get("/health", self.health)
        app.router.add_get("/v1/models", self.models)
        app.router.add_post("/v1/chat/completions", self.chat_completions)
        app.router.add_post("/v1/messages", self.anthropic_messages)
        app.router.add_post("/v1/responses", self.responses)
        app.router.add_post("/v1/responses/compact", self.responses_compact)
        app.router.add_get("/picker", self.picker_page)
        app.router.add_get("/api/models", self.api_models)
        app.router.add_post("/api/switch", self.switch_model)
        return app

    async def picker_page(self, _request: web.Request) -> web.Response:
        return web.Response(text=_picker_html(self.picker_token), content_type="text/html")

    async def api_models(self, _request: web.Request) -> web.Response:
        current = _current_managed_model()
        data: list[dict[str, Any]] = []
        router_config = self._active_router()
        if router_config is not None:
            data.append(
                {
                    "slug": router_config.slug,
                    "display_name": router_config.display_name,
                    "provider": "auto",
                    "active": current == router_config.slug,
                }
            )
        if chatgpt_passthrough_available():
            for slug, display_name in chatgpt_passthrough_display_names().items():
                data.append(
                    {
                        "slug": slug,
                        "display_name": display_name,
                        "provider": "chatgpt",
                        "active": current == slug,
                    }
                )
        if cursor_passthrough_available():
            for slug, display_name in cursor_passthrough_display_names().items():
                data.append(
                    {
                        "slug": slug,
                        "display_name": display_name,
                        "provider": "cursor",
                        "active": current == slug,
                    }
                )
        for m in usable_byok_models(self.settings.load()):
            data.append(
                {
                    "slug": m.slug,
                    "display_name": m.display_name,
                    "provider": m.provider,
                    "active": current == m.slug,
                }
            )
        return web.json_response(data)

    def _valid_picker_token(self, request: web.Request) -> bool:
        token = request.headers.get(PICKER_TOKEN_HEADER, "")
        return secrets.compare_digest(token, self.picker_token)

    async def switch_model(self, request: web.Request) -> web.Response:
        if not self._valid_picker_token(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        slug = str(body.get("slug") or "").strip()
        if not slug:
            return web.json_response({"error": "slug is required"}, status=400)
        models = usable_byok_models(self.settings.load())
        valid = {m.slug for m in models}
        display_for: dict[str, str] = {m.slug: m.display_name for m in models}
        router_config = self._active_router()
        if router_config is not None:
            valid.add(router_config.slug)
            display_for[router_config.slug] = router_config.display_name
        if chatgpt_passthrough_available():
            valid.update(chatgpt_passthrough_slugs())
            display_for.update(chatgpt_passthrough_display_names())
        if cursor_passthrough_available():
            valid.update(cursor_passthrough_display_names())
            display_for.update(cursor_passthrough_display_names())
        if slug not in valid:
            return web.json_response({"error": f"unknown model: {slug}"}, status=404)
        _set_active_model(slug, display_for.get(slug, slug))
        restart = bool(body.get("restart_codex"))
        if restart:
            _restart_codex_app()
        return web.json_response({"ok": True, "model": slug, "restarted": restart})

    async def health(self, _request: web.Request) -> web.Response:
        models = usable_byok_models(self.settings.load())
        chatgpt_ok = chatgpt_passthrough_available()
        cursor_ok = cursor_passthrough_available()
        passthrough_count = len(chatgpt_passthrough_slugs()) if chatgpt_ok else 0
        if cursor_ok:
            passthrough_count += len(cursor_passthrough_display_names())
        count = len(models) + passthrough_count
        return web.json_response(
            {
                "ok": True,
                "models": count,
                "chatgpt_passthrough": chatgpt_ok,
                "cursor_passthrough": cursor_ok,
                "auto_router": self._active_router() is not None,
            }
        )

    async def models(self, _request: web.Request) -> web.Response:
        now = int(time.time())
        data: list[dict[str, Any]] = []
        router_config = self._active_router()
        if router_config is not None:
            data.append(router_module.router_models_entry(router_config, now))
        if chatgpt_passthrough_available():
            data.extend(
                {"id": slug, "object": "model", "created": now, "owned_by": "chatgpt"}
                for slug in sorted(chatgpt_passthrough_slugs())
            )
        if cursor_passthrough_available():
            data.extend(
                {
                    "id": slug,
                    "object": "model",
                    "created": now,
                    "owned_by": "cursor",
                }
                for slug in sorted(cursor_passthrough_display_names())
            )
        data.extend({"id": model.slug, "object": "model", "created": now, "owned_by": "codex-shim"} for model in usable_byok_models(self.settings.load()))
        return web.json_response({"object": "list", "data": data})

    async def chat_completions(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        body = await self._maybe_apply_auto_router(body)
        route = self._route(body)
        if route.is_openai_chat:
            forwarded = dict(body)
            forwarded["model"] = route.model
            if "messages" in forwarded:
                forwarded["messages"] = _normalize_roles(forwarded["messages"])
            return await self._post_openai_chat(request, route, forwarded, as_responses=False)
        if route.is_anthropic:
            forwarded = chat_to_anthropic(body, route.model, route.max_output_tokens)
            return await self._post_anthropic(request, route, forwarded, as_responses=False)
        raise web.HTTPBadGateway(text=f"Unsupported model provider: {route.provider}")

    async def anthropic_messages(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        route = self._route(body)
        if route.is_openai_chat:
            forwarded = anthropic_messages_to_chat(body, route.model, route.max_output_tokens)
            return await self._post_openai_chat_as_anthropic(request, route, forwarded)
        if route.is_anthropic:
            forwarded = dict(body)
            forwarded["model"] = route.model
            return await self._post_anthropic_messages(request, route, forwarded)
        raise web.HTTPBadGateway(text=f"Unsupported model provider: {route.provider}")

    async def responses(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        _log_incoming_request("/v1/responses", body)
        body = await self._maybe_apply_auto_router(body)
        model = str(body.get("model") or "")
        if is_chatgpt_passthrough_slug(model):
            upstream = chatgpt_upstream_model(model)
            override = model if model != upstream else None
            return await self._chatgpt_passthrough(
                request,
                body,
                response_model_override=override,
                upstream_model=upstream,
            )
        if is_cursor_passthrough_slug(model):
            return await self._cursor_passthrough(
                request,
                body,
                response_model_override=model,
                upstream_model=cursor_upstream_model(model),
            )
        if self._needs_image_gen(body) or self._needs_image_followup(body):
            return await self._chatgpt_passthrough(request, body, response_model_override=model)
        route = self._route(body)
        if route.is_openai_chat:
            forwarded = responses_to_chat(body, route.model)
            return await self._post_openai_chat(request, route, forwarded, as_responses=True)
        if route.is_anthropic:
            forwarded = responses_to_anthropic(body, route.model, route.max_output_tokens)
            return await self._post_anthropic(request, route, forwarded, as_responses=True)
        raise web.HTTPBadGateway(text=f"Unsupported model provider: {route.provider}")

    async def responses_compact(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        _log_incoming_request("/v1/responses/compact", body)
        body = await self._maybe_apply_auto_router(body)
        model = str(body.get("model") or "")
        if is_chatgpt_passthrough_slug(model):
            upstream = chatgpt_upstream_model(model)
            return await self._chatgpt_compact_passthrough(request, body, upstream_model=upstream)
        if is_cursor_passthrough_slug(model):
            compact_body = dict(body)
            compact_body["input"] = body.get("input") or []
            compact_body["instructions"] = (
                f"{body.get('instructions') or ''}\n\nSummarize the conversation above into a compact "
                "context window suitable for continuing the task."
            ).strip()
            return await self._cursor_passthrough(
                request,
                compact_body,
                response_model_override=model,
                upstream_model=cursor_upstream_model(model),
                force_non_stream=True,
            )
        route = self._route(body)
        compact_body = _compact_request_body(body, route.model)
        if route.is_openai_chat:
            forwarded = responses_to_chat(compact_body, route.model)
            forwarded["stream"] = False
            response = await self._post_openai_chat(request, route, forwarded, as_responses=True)
            return await _as_compact_response(response, route.slug)
        if route.is_anthropic:
            forwarded = responses_to_anthropic(compact_body, route.model, route.max_output_tokens)
            forwarded["stream"] = False
            response = await self._post_anthropic(request, route, forwarded, as_responses=True)
            return await _as_compact_response(response, route.slug)
        raise web.HTTPBadGateway(text=f"Unsupported model provider: {route.provider}")

    def _needs_image_gen(self, body: dict[str, Any]) -> bool:
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
            is_image_tool = tool_type in {"image_generation", "image_gen"} or ("image" in normalized and "gen" in normalized)
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
            fn = tool_choice.get("function") or {}
            choice_name = str(tool_choice.get("name") or (fn.get("name") if isinstance(fn, dict) else "") or tool_choice.get("type") or "").lower()
            if any(name.lower() in choice_name for name in image_tool_names):
                return True

        if non_image_tool_count == 0:
            return True

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
        if isinstance(inputs, str):
            return inputs
        if not isinstance(inputs, list):
            return ""
        for item in reversed(inputs):
            if isinstance(item, str):
                return item
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
            return "\n".join(part for part in parts if part)
        if isinstance(content, dict):
            return str(content.get("text") or content.get("content") or "")
        return str(content)

    async def _chatgpt_passthrough(
        self,
        request: web.Request,
        body: dict[str, Any],
        response_model_override: str | None = None,
        upstream_model: str | None = None,
    ) -> web.StreamResponse:
        """Forward a Responses request to chatgpt.com using the user's Codex auth.

        Lets the picker expose OpenAI GPT models (ChatGPT subscription) as
        first-class models alongside configured BYOK entries.
        """
        auth_path = DEFAULT_CODEX_AUTH.expanduser()
        try:
            auth = json.loads(auth_path.read_text())
        except FileNotFoundError:
            raise web.HTTPUnauthorized(text="~/.codex/auth.json not found")
        tokens = auth.get("tokens") or {}
        access_token = tokens.get("access_token")
        account_id = tokens.get("account_id") or ""
        if not access_token:
            raise web.HTTPUnauthorized(text="auth.json has no access_token")
        forwarded = _sanitize_chatgpt_passthrough_body(body)
        forwarded["model"] = upstream_model or CHATGPT_MODEL_SLUG
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

    async def _chatgpt_compact_passthrough(
        self,
        request: web.Request,
        body: dict[str, Any],
        upstream_model: str | None = None,
    ) -> web.StreamResponse:
        auth_path = DEFAULT_CODEX_AUTH.expanduser()
        try:
            auth = json.loads(auth_path.read_text())
        except FileNotFoundError:
            raise web.HTTPUnauthorized(text="~/.codex/auth.json not found")
        tokens = auth.get("tokens") or {}
        access_token = tokens.get("access_token")
        account_id = tokens.get("account_id") or ""
        if not access_token:
            raise web.HTTPUnauthorized(text="auth.json has no access_token")
        forwarded = _sanitize_chatgpt_passthrough_body(body)
        original_model = str(forwarded.get("model") or "")
        forwarded["model"] = upstream_model or CHATGPT_MODEL_SLUG
        forwarded.pop("stream", None)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "OpenAI-Beta": "responses=2026-02-06",
            "originator": "codex_cli_rs",
            "chatgpt-account-id": account_id,
            "session_id": request.headers.get("session_id", ""),
        }
        url = "https://chatgpt.com/backend-api/codex/responses/compact"
        async with ClientSession(timeout=self.timeout) as session:
            upstream = await session.post(url, json=forwarded, headers=headers)
            if upstream.status >= 400:
                return await _error_response(upstream)
            payload = await upstream.json(content_type=None)
        _rewrite_response_model(payload, original_model or None)
        return web.json_response(payload)

    async def _cursor_passthrough(
        self,
        request: web.Request,
        body: dict[str, Any],
        response_model_override: str | None = None,
        upstream_model: str | None = None,
        force_non_stream: bool = False,
    ) -> web.StreamResponse:
        """Route Composer through cursor-agent using Cursor subscription login."""
        if not cursor_passthrough_available():
            raise web.HTTPUnauthorized(
                text="Cursor subscription auth unavailable. Run `cursor-agent login`, then retry."
            )
        slug = response_model_override or CURSOR_MODEL_SLUG
        upstream = upstream_model or cursor_upstream_model(slug)
        prompt = build_cursor_prompt(body)
        stream = bool(body.get("stream")) and not force_non_stream

        if not stream:
            text = ""
            usage: dict[str, Any] | None = None
            async for event in iter_cursor_agent_events(prompt, upstream):
                if event["type"] == "completed":
                    text = str(event.get("text") or text)
                elif event["type"] == "usage":
                    usage = event.get("usage") if isinstance(event.get("usage"), dict) else None
                elif event["type"] == "error":
                    raise web.HTTPBadGateway(text=str(event.get("message") or "cursor-agent failed"))
            payload: dict[str, Any] = {
                "id": f"resp_{int(time.time() * 1000)}",
                "object": "response",
                "model": slug,
                "status": "completed",
                "output": [
                    {
                        "id": "msg_0",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                ],
            }
            normalized_usage = normalize_responses_usage(usage)
            if normalized_usage:
                payload["usage"] = normalized_usage
            return web.json_response(payload)

        response = _sse_response()
        await response.prepare(request)
        tool_types = _build_tool_types(body)
        state = ResponsesStreamState(slug, tool_types)
        try:
            await state.start(response)
            async for event in iter_cursor_agent_events(prompt, upstream):
                if event["type"] == "text_delta":
                    await state.write_chat_delta(
                        response,
                        {"choices": [{"delta": {"content": event["delta"]}}]},
                    )
                elif event["type"] == "usage":
                    normalized_usage = normalize_responses_usage(event.get("usage"))
                    if normalized_usage:
                        state.usage = normalized_usage
                elif event["type"] == "error":
                    message = str(event.get("message") or "cursor-agent failed")
                    await state.write_chat_delta(
                        response,
                        {"choices": [{"delta": {"content": message}}]},
                    )
                    break
            await state.finish(response)
        except ClientDisconnected:
            pass
        except Exception as exc:
            print(f"[err] cursor passthrough {slug}: {exc}", flush=True)
            raise web.HTTPBadGateway(text=str(exc)) from exc
        try:
            await response.write_eof()
        except Exception:
            pass
        return response

    # ------------------------------------------------------------------
    # Auto Router
    # ------------------------------------------------------------------
    def _active_router(self):
        """Return the RouterConfig only when enabled and at least one candidate
        backend is usable, so discovery never advertises a dead Auto entry."""
        config = self.settings.load_router()
        if config and router_module.router_is_active(config, available_model_slugs(self.settings.load())):
            return config
        return None

    async def _maybe_apply_auto_router(self, body: dict[str, Any]) -> dict[str, Any]:
        """If the request targets the Auto Router slug, classify the task and
        rewrite ``model`` to the concrete backend that should handle it. Any
        failure leaves the body untouched so the request still routes normally."""
        config = self.settings.load_router()
        if not config or not config.effective_enabled:
            return body
        if str(body.get("model") or "") != config.slug:
            return body
        resolved = await self._resolve_auto_model(config, body)
        if resolved and resolved != config.slug:
            if router_module.router_log_enabled():
                print(f"[router] {config.slug} -> {resolved}", flush=True)
            new_body = dict(body)
            new_body["model"] = resolved
            return new_body
        return body

    async def _resolve_auto_model(self, config, body: dict[str, Any]) -> str | None:
        models = self.settings.load()
        candidates = router_module.filter_available(config, available_model_slugs(models))
        if not candidates:
            return None
        classify = None
        if config.classifier:
            classifier_model = self.settings.by_slug_or_model(config.classifier)
            if (
                classifier_model is not None
                and byok_model_has_credentials(classifier_model)
                and (classifier_model.is_openai_chat or classifier_model.is_anthropic)
            ):
                classify = self._make_classifier(classifier_model, config)
        log = (lambda message: print(message, flush=True)) if router_module.router_log_enabled() else None
        resolved, _info = await router_module.resolve_auto(config, candidates, body, classify, log=log)
        return resolved or router_module.fallback_slug(
            config, candidates, has_image_task=router_module.has_images(body)
        )

    def _make_classifier(self, model: ShimModel, config):
        timeout = ClientTimeout(total=config.timeout + 5, sock_connect=config.timeout, sock_read=config.timeout)

        async def classify(system_prompt: str, user_content: str) -> str:
            async with ClientSession(timeout=timeout) as session:
                if model.is_anthropic:
                    url = _join_url(model.base_url, "/messages")
                    payload = {
                        "model": model.model,
                        "max_tokens": config.max_tokens,
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": user_content}],
                    }
                    upstream = await session.post(url, json=payload, headers=_anthropic_headers(model))
                    upstream.raise_for_status()
                    data = await upstream.json(content_type=None)
                    return _anthropic_text(data)
                url = _join_url(model.base_url, "/chat/completions")
                payload = {
                    "model": model.model,
                    "stream": False,
                    "temperature": 0,
                    "max_tokens": config.max_tokens,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                }
                upstream = await session.post(url, json=payload, headers=_openai_headers(model))
                upstream.raise_for_status()
                data = await upstream.json(content_type=None)
                message = (data.get("choices") or [{}])[0].get("message") or {}
                return str(message.get("content") or "")

        return classify

    def _route(self, body: dict[str, Any]) -> ShimModel:
        requested = str(body.get("model") or "")
        route = self.settings.by_slug_or_model(requested)
        if route is None:
            raise web.HTTPNotFound(text=f"Unknown model slug/model: {requested}")
        if not byok_model_has_credentials(route):
            raise web.HTTPUnauthorized(text=_missing_api_key_message(route))
        return route

    async def _post_openai_chat(
        self, request: web.Request, route: ShimModel, body: dict[str, Any], as_responses: bool
    ) -> web.StreamResponse:
        url = _join_url(route.base_url, "/chat/completions")
        headers = _openai_headers(route)
        _dump_debug_request(route.slug, url, body)
        async with ClientSession(timeout=self.timeout) as session:
            upstream = await session.post(url, json=body, headers=headers)
            if upstream.status >= 400:
                return await _error_response(upstream, slug=route.slug)
            if body.get("stream"):
                return await self._stream_openai_chat(request, upstream, route, as_responses, body)
            payload = await upstream.json(content_type=None)
        if as_responses:
            tool_types = _build_tool_types(body)
            payload = chat_completion_to_response(payload, route.slug, tool_types)
            intercepted = _maybe_intercept_web_search(payload)
            return web.json_response(intercepted or payload)
        return web.json_response(payload)

    async def _post_openai_chat_as_anthropic(
        self, request: web.Request, route: ShimModel, body: dict[str, Any]
    ) -> web.StreamResponse:
        url = _join_url(route.base_url, "/chat/completions")
        headers = _openai_headers(route)
        _dump_debug_request(route.slug, url, body)
        async with ClientSession(timeout=self.timeout) as session:
            upstream = await session.post(url, json=body, headers=headers)
            if upstream.status >= 400:
                return await _anthropic_error_response(upstream)
            if body.get("stream"):
                return await self._stream_openai_chat_as_anthropic(request, upstream, route)
            payload = await upstream.json(content_type=None)
        return web.json_response(chat_completion_to_anthropic_message(payload, route.slug))

    async def _post_anthropic(
        self, request: web.Request, route: ShimModel, body: dict[str, Any], as_responses: bool
    ) -> web.StreamResponse:
        url = _join_url(route.base_url, "/messages")
        headers = _anthropic_headers(route)
        async with ClientSession(timeout=self.timeout) as session:
            upstream = await session.post(url, json=body, headers=headers)
            if upstream.status >= 400:
                return await _error_response(upstream)
            if body.get("stream"):
                return await self._stream_anthropic(request, upstream, route, as_responses, body)
            payload = await upstream.json(content_type=None)
        if as_responses:
            tool_types = _build_tool_types(body)
            payload = anthropic_to_response(payload, route.slug, tool_types)
            intercepted = _maybe_intercept_web_search(payload)
            return web.json_response(intercepted or payload)
        return web.json_response(anthropic_to_chat_response(payload, route.slug))

    async def _post_anthropic_messages(
        self, request: web.Request, route: ShimModel, body: dict[str, Any]
    ) -> web.StreamResponse:
        url = _join_url(route.base_url, "/messages")
        headers = _anthropic_headers(route)
        async with ClientSession(timeout=self.timeout) as session:
            upstream = await session.post(url, json=body, headers=headers)
            if upstream.status >= 400:
                return await _error_response(upstream, slug=route.slug)
            if body.get("stream"):
                return await self._stream_raw_sse(request, upstream, route.slug)
            payload = await upstream.json(content_type=None)
        if isinstance(payload, dict):
            payload["model"] = route.slug
        return web.json_response(payload)

    async def _stream_openai_chat(
        self, request: web.Request, upstream, route: ShimModel, as_responses: bool, body: dict[str, Any] | None = None
    ) -> web.StreamResponse:
        response = _sse_response()
        await response.prepare(request)
        if as_responses:
            tool_types = _build_tool_types(body) if body else {}
            state = ResponsesStreamState(route.slug, tool_types)
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

    async def _stream_openai_chat_as_anthropic(
        self, request: web.Request, upstream, route: ShimModel
    ) -> web.StreamResponse:
        response = _sse_response()
        await response.prepare(request)
        state = AnthropicMessagesStreamState(route.slug)
        try:
            await state.start(response)
            async for line in _sse_lines(upstream):
                if line == "[DONE]":
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await state.write_chat_delta(response, event)
            await state.finish(response)
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
        self, request: web.Request, upstream, route: ShimModel, as_responses: bool, body: dict[str, Any] | None = None
    ) -> web.StreamResponse:
        response = _sse_response()
        await response.prepare(request)
        if as_responses:
            tool_types = _build_tool_types(body) if body else {}
            state = ResponsesStreamState(route.slug, tool_types)
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

    async def _stream_raw_sse(self, request: web.Request, upstream, model_slug: str | None = None) -> web.StreamResponse:
        response = _sse_response()
        await response.prepare(request)
        try:
            async for line in _sse_lines(upstream):
                if model_slug and line.startswith("{"):
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and event.get("type") == "message_start":
                            msg = event.get("message")
                            if isinstance(msg, dict):
                                msg["model"] = model_slug
                        await _write_anthropic_sse(response, event.get("type", "message"), event)
                        continue
                    except json.JSONDecodeError:
                        pass
                await _safe_write(response, f"data: {line}\n\n".encode())
        except ClientDisconnected:
            pass
        finally:
            upstream.release()
        try:
            await response.write_eof()
        except Exception:
            pass
        return response


_DROP_ITEM = object()


def _sanitize_chatgpt_passthrough_body(body: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_chatgpt_passthrough_value(body)
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_chatgpt_passthrough_value(value: Any) -> Any:
    if isinstance(value, list):
        output = []
        for item in value:
            sanitized = _sanitize_chatgpt_passthrough_value(item)
            if sanitized is not _DROP_ITEM:
                output.append(sanitized)
        return output
    if isinstance(value, dict):
        if value.get("type") == "reasoning" and _has_shim_encrypted_content(value):
            return _DROP_ITEM
        output = {}
        for key, item in value.items():
            if key == "encrypted_content" and isinstance(item, str) and item.startswith(SHIM_ENCRYPTED_CONTENT_PREFIX):
                continue
            sanitized = _sanitize_chatgpt_passthrough_value(item)
            if sanitized is not _DROP_ITEM:
                output[key] = sanitized
        return output
    return value


def _has_shim_encrypted_content(value: dict[str, Any]) -> bool:
    encrypted_content = value.get("encrypted_content")
    return isinstance(encrypted_content, str) and encrypted_content.startswith(SHIM_ENCRYPTED_CONTENT_PREFIX)


def _rewrite_response_model(payload: Any, model: str | None) -> None:
    if not model:
        return
    if isinstance(payload, dict):
        if payload.get("model") == CHATGPT_MODEL_SLUG:
            payload["model"] = model
        for value in payload.values():
            _rewrite_response_model(value, model)
    elif isinstance(payload, list):
        for item in payload:
            _rewrite_response_model(item, model)


class AnthropicMessagesStreamState:
    """Translates OpenAI chat-completions chunks into Anthropic Messages SSE."""

    def __init__(self, model: str):
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.model = model
        self.next_index = 0
        self.text_index: int | None = None
        self.reasoning_index: int | None = None
        self.text_open = False
        self.reasoning_open = False
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] | None = None
        self.stop_reason = "end_turn"

    async def start(self, response: web.StreamResponse) -> None:
        await _write_anthropic_sse(
            response,
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    async def write_chat_delta(self, response: web.StreamResponse, chunk: dict[str, Any]) -> None:
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage = normalize_responses_usage(usage)
        choice = (chunk.get("choices") or [{}])[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            self.stop_reason = _chat_finish_to_anthropic_stop(finish_reason)
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            await self._reasoning_delta(response, str(reasoning))
        content = delta.get("content")
        if content:
            if self.reasoning_open:
                await self._close_reasoning(response)
            await self._text_delta(response, str(content))
        for call in delta.get("tool_calls") or []:
            await self._tool_delta(response, call)

    async def finish(self, response: web.StreamResponse) -> None:
        if self.reasoning_open:
            await self._close_reasoning(response)
        if self.text_open:
            await self._close_text(response)
        for index in sorted(self.tool_calls):
            state = self.tool_calls[index]
            if not state.get("closed"):
                await self._close_tool(response, index, state)
        await _write_anthropic_sse(
            response,
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
                "usage": _responses_usage_to_anthropic_usage(self.usage) or {"output_tokens": 0},
            },
        )
        await _write_anthropic_sse(response, "message_stop", {"type": "message_stop"})

    async def _text_delta(self, response: web.StreamResponse, text: str) -> None:
        if self.text_index is None:
            self.text_index = self.next_index
            self.next_index += 1
            self.text_open = True
            await _write_anthropic_sse(
                response,
                "content_block_start",
                {"type": "content_block_start", "index": self.text_index, "content_block": {"type": "text", "text": ""}},
            )
        await _write_anthropic_sse(
            response,
            "content_block_delta",
            {"type": "content_block_delta", "index": self.text_index, "delta": {"type": "text_delta", "text": text}},
        )

    async def _close_text(self, response: web.StreamResponse) -> None:
        if self.text_index is None:
            return
        await _write_anthropic_sse(response, "content_block_stop", {"type": "content_block_stop", "index": self.text_index})
        self.text_index = None
        self.text_open = False

    async def _reasoning_delta(self, response: web.StreamResponse, text: str) -> None:
        if self.reasoning_index is None:
            self.reasoning_index = self.next_index
            self.next_index += 1
            self.reasoning_open = True
            await _write_anthropic_sse(
                response,
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self.reasoning_index,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            )
        await _write_anthropic_sse(
            response,
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.reasoning_index,
                "delta": {"type": "thinking_delta", "thinking": text},
            },
        )

    async def _close_reasoning(self, response: web.StreamResponse) -> None:
        if self.reasoning_index is None:
            return
        await _write_anthropic_sse(
            response,
            "content_block_stop",
            {"type": "content_block_stop", "index": self.reasoning_index},
        )
        self.reasoning_index = None
        self.reasoning_open = False

    async def _tool_delta(self, response: web.StreamResponse, call: dict[str, Any]) -> None:
        index = int(call.get("index", 0))
        fn = call.get("function") or {}
        state = self.tool_calls.setdefault(
            index,
            {
                "id": "",
                "name": "",
                "arguments": "",
                "emitted": 0,
                "block_index": None,
                "open": False,
                "closed": False,
            },
        )
        if call.get("id"):
            state["id"] = call["id"]
        if fn.get("name"):
            state["name"] += fn["name"]
        if fn.get("arguments"):
            state["arguments"] += fn["arguments"]
        if not state["open"] and state["name"]:
            if self.reasoning_open:
                await self._close_reasoning(response)
            if self.text_open:
                await self._close_text(response)
            await self._open_tool(response, index, state)
        if state["open"] and len(state["arguments"]) > state["emitted"]:
            delta = state["arguments"][state["emitted"] :]
            state["emitted"] = len(state["arguments"])
            await _write_anthropic_sse(
                response,
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": state["block_index"],
                    "delta": {"type": "input_json_delta", "partial_json": delta},
                },
            )

    async def _open_tool(self, response: web.StreamResponse, index: int, state: dict[str, Any]) -> None:
        state["block_index"] = self.next_index
        self.next_index += 1
        state["open"] = True
        if not state["id"]:
            state["id"] = f"call_{index}"
        await _write_anthropic_sse(
            response,
            "content_block_start",
            {
                "type": "content_block_start",
                "index": state["block_index"],
                "content_block": {
                    "type": "tool_use",
                    "id": state["id"],
                    "name": state["name"] or "tool",
                    "input": {},
                },
            },
        )

    async def _close_tool(self, response: web.StreamResponse, index: int, state: dict[str, Any]) -> None:
        if not state["open"]:
            await self._open_tool(response, index, state)
            if len(state["arguments"]) > state["emitted"]:
                delta = state["arguments"][state["emitted"] :]
                state["emitted"] = len(state["arguments"])
                await _write_anthropic_sse(
                    response,
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": state["block_index"],
                        "delta": {"type": "input_json_delta", "partial_json": delta},
                    },
                )
        await _write_anthropic_sse(
            response,
            "content_block_stop",
            {"type": "content_block_stop", "index": state["block_index"]},
        )
        state["open"] = False
        state["closed"] = True


class ResponsesStreamState:
    """Translates upstream chat-completions / anthropic stream events into the
    Codex Desktop Responses-API event sequence. Keeps the message item and
    each tool call as separate output items with stable indices, and emits
    proper .added / .delta / .done / .completed events plus a final
    `response.completed` with the full reconciled `output` array."""

    def __init__(self, model: str, tool_types: dict[str, str] | None = None):
        self.response_id = f"resp_{int(time.time() * 1000)}"
        self.message_item_id = f"msg_{int(time.time() * 1000)}"
        self.model = model
        self.message_index: int | None = None
        self.message_text = ""
        self.message_opened = False
        self.message_closed = False
        self.usage: dict[str, Any] | None = None
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.reasoning_blocks: dict[Any, dict[str, Any]] = {}
        self.next_output_index = 0
        # Map sanitized tool name -> original Responses tool type so we can
        # emit the correct output item type (e.g. custom_tool_call for freeform
        # apply_patch instead of generic function_call).
        self.tool_types = tool_types or {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self, response: web.StreamResponse) -> None:
        await _write_sse(response, {"type": "response.created", "response": self._response("in_progress")})

    async def finish(self, response: web.StreamResponse) -> None:
        for state in sorted(self.reasoning_blocks.values(), key=lambda s: s["output_index"]):
            if not state.get("closed"):
                await self._close_reasoning(response, state)
        if self.message_opened and not self.message_closed:
            await self._close_message(response)
        for state in sorted(self.tool_calls.values(), key=lambda s: s["output_index"]):
            if not state.get("closed"):
                await self._close_tool(response, state)
        await _write_sse(response, {"type": "response.completed", "response": self._response("completed", final=True)})
        await response.write(b"data: [DONE]\n\n")

    # ------------------------------------------------------------------
    # Chat-completions (OpenAI-style) deltas
    # ------------------------------------------------------------------
    async def write_chat_delta(self, response: web.StreamResponse, chunk: dict[str, Any]) -> None:
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage = normalize_responses_usage(usage)
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            await self._chat_reasoning_delta(response, reasoning)
        content = delta.get("content")
        if content:
            for state in list(self.reasoning_blocks.values()):
                if not state.get("closed"):
                    await self._close_reasoning(response, state)
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
        if event_type == "message_start":
            message = event.get("message") or {}
            usage = message.get("usage")
            if isinstance(usage, dict):
                self.usage = normalize_responses_usage(usage)
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
        elif event_type == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, dict):
                if self.usage is None or any(
                    key in usage for key in ("input_tokens", "prompt_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                ):
                    normalized = normalize_responses_usage(usage)
                    if normalized is not None:
                        self.usage = normalized if self.usage is None else {**self.usage, **normalized}
                output_tokens = usage.get("output_tokens")
                if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
                    if self.usage is None:
                        self.usage = normalize_responses_usage(usage)
                    else:
                        self.usage["output_tokens"] = output_tokens
                        self.usage["total_tokens"] = int(self.usage.get("input_tokens") or 0) + output_tokens
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
        # Determine output item type based on original tool type.
        # Freeform tools (apply_patch with no schema) emit custom_tool_call
        # so Codex Desktop knows not to validate against a fixed enum.
        original_type = self.tool_types.get(name, "")
        output_type = "function_call"
        if original_type == "apply_patch":
            output_type = "custom_tool_call"
        elif original_type.startswith("web_search"):
            output_type = "web_search_call"
        state: dict[str, Any] = {
            "id": call_id,
            "call_id": call_id,
            "name": name,
            "arguments": "",
            "output_index": output_index,
            "closed": False,
            "output_type": output_type,
        }
        self.tool_calls[key] = state
        await _write_sse(
            response,
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {
                    "id": call_id,
                    "type": output_type,
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
            "type": state.get("output_type", "function_call"),
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
        payload = {
            "id": self.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": self.model,
            "output": output,
        }
        if self.usage is not None:
            payload["usage"] = self.usage
        elif final:
            payload["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        return payload


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


def _build_tool_types(body: dict[str, Any]) -> dict[str, str]:
    """Build a map sanitized tool name -> original tool type from the request tools array.

    Codex Desktop emits native tools like `{"type": "apply_patch"}` and MCP tools
    like `{"type": "mcp__node_repl", "function": {"name": "js"}}`. When we translate
    those into chat-completions `function` tools, the original type is lost. We
    preserve it here so the Responses streaming translator can emit the correct
    output item type (e.g. `custom_tool_call` for freeform apply_patch instead of
    generic `function_call`).
    """
    tool_types: dict[str, str] = {}
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tool_type = str(tool.get("type") or "").strip().lower()
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            name = str(fn["name"]).strip()
        elif tool.get("name"):
            name = str(tool["name"]).strip()
        else:
            name = tool_type
        clean = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip())[:64].strip("_")
        if clean:
            tool_types[clean] = tool_type
    return tool_types

async def _perform_web_search(query: str) -> str:
    """Execute a web search via DuckDuckGo and return text results.

    This is a server-side fallback for custom models whose provider does not
    have a native web-search capability.  Codex Desktop expects the shim to
    return results as a `function_call_output` (or `web_search_call`) item;
    when the model is BYOK, the Desktop app does not execute the search itself,
    so the shim must do it and feed the results back into the conversation.
    """
    import urllib.parse
    import urllib.request

    if not query or not query.strip():
        return "No search query provided."

    # DuckDuckGo lite HTML endpoint (no API key required)
    url = (
        "https://html.duckduckgo.com/html/"
        + "?q="
        + urllib.parse.quote_plus(query.strip())
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return f"Web search failed: {exc}"

    # Extract title + snippet from result links
    results: list[str] = []
    # Each result is in a `.result` div with `.result__a` (title/link) and `.result__snippet`
    from html.parser import HTMLParser

    class _ResultParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.in_result = False
            self.in_a = False
            self.in_snippet = False
            self.current_title = ""
            self.current_snippet = ""
            self.results: list[dict[str, str]] = []
            self._tag_stack: list[str] = []
            self._class_stack: list[str] = []

        def _current_class(self) -> str:
            return self._class_stack[-1] if self._class_stack else ""

        def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
            attrs = dict(attrs_list)
            cls = (attrs.get("class") or "").lower()
            self._tag_stack.append(tag)
            self._class_stack.append(cls)
            if "result" in cls and tag == "div":
                self.in_result = True
                self.current_title = ""
                self.current_snippet = ""
            if self.in_result and tag == "a" and "result__a" in cls:
                self.in_a = True
            if self.in_result and ("result__snippet" in cls or "result__body" in cls):
                self.in_snippet = True

        def handle_endtag(self, tag: str) -> None:
            if self._tag_stack and self._tag_stack[-1] == tag:
                self._tag_stack.pop()
                self._class_stack.pop()
            if tag == "div" and self.in_result:
                if self.current_title or self.current_snippet:
                    self.results.append(
                        {
                            "title": self.current_title.strip(),
                            "snippet": self.current_snippet.strip(),
                        }
                    )
                self.in_result = False
            if tag == "a":
                self.in_a = False
            if tag in {"div", "span", "p"}:
                self.in_snippet = False

        def handle_data(self, data: str) -> None:
            if self.in_a:
                self.current_title += data
            if self.in_snippet:
                self.current_snippet += data

    parser = _ResultParser()
    parser.feed(html)
    for r in parser.results[:5]:
        title = r["title"].replace("\n", " ")
        snippet = r["snippet"].replace("\n", " ")
        if title and snippet:
            results.append(f"{title}\n{snippet}")
        elif title:
            results.append(title)
        elif snippet:
            results.append(snippet)

    if not results:
        return "No web search results found."
    return "\n\n".join(results)

def _maybe_intercept_web_search(payload: dict[str, Any]) -> dict[str, Any] | None:
    """If the response payload contains a web_search_call, execute it server-side
    and return a new payload with the results embedded as a function_call_output.

    Returns None if no web_search_call is present (pass through unchanged).
    """
    output = payload.get("output") or []
    if not isinstance(output, list):
        return None
    search_calls: list[tuple[int, dict[str, Any]]] = []
    for i, item in enumerate(output):
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            search_calls.append((i, item))
    if not search_calls:
        return None

    # Build synthetic search results
    results: list[dict[str, Any]] = []
    for idx, call in search_calls:
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        query = args.get("query") or ""
        # Run the search synchronously (non-streaming path only)
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            result_text = loop.run_until_complete(_perform_web_search(query))
        except RuntimeError:
            result_text = "Web search unavailable in this context."
        results.append({
            "id": f"wso_{call.get('call_id', '0')}",
            "type": "function_call_output",
            "status": "completed",
            "call_id": call.get("call_id"),
            "output": result_text,
        })

    # Replace web_search_call items with their results
    new_output: list[dict[str, Any]] = []
    for i, item in enumerate(output):
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            # Find matching result
            for r in results:
                if r.get("call_id") == item.get("call_id"):
                    new_output.append(r)
                    break
            else:
                new_output.append(item)
        else:
            new_output.append(item)

    new_payload = dict(payload)
    new_payload["output"] = new_output
    return new_payload


_VERSIONED_BASE_RE = re.compile(r"(?:^|/)v\d+$")


def _join_url(base_url: str, endpoint: str) -> str:
    base = base_url.rstrip("/")
    if _VERSIONED_BASE_RE.search(base):
        # Already ends with /v<n> (e.g. /v1, /api/coding/v3) — append
        # the endpoint as-is rather than injecting another /v1/.
        return base + endpoint
    if endpoint == "/messages":
        return base + "/v1/messages"
    return urljoin(base + "/", "v1" + endpoint)


def _openai_headers(route: ShimModel) -> dict[str, str]:
    headers = {"Content-Type": "application/json", **route.extra_headers}
    if route.api_key:
        headers.setdefault("Authorization", f"Bearer {route.api_key}")
    return headers


def _anthropic_headers(route: ShimModel) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        **route.extra_headers,
    }
    if route.api_key:
        headers.setdefault("x-api-key", route.api_key)
    return headers


def _anthropic_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in (payload.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts)


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


async def _write_anthropic_sse(response: web.StreamResponse, event: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, separators=(",", ":"))
    try:
        await response.write(f"event: {event}\ndata: {data}\n\n".encode())
    except (ConnectionResetError, ConnectionError) as exc:
        raise ClientDisconnected() from exc
    except Exception as exc:
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


def _compact_request_body(body: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    instructions = body.get("instructions") or _default_compact_instructions()
    return {
        "model": upstream_model,
        "instructions": instructions,
        "input": body.get("input") or [],
        "max_output_tokens": body.get("max_output_tokens") or body.get("max_tokens") or 4096,
        "stream": False,
    }


def _default_compact_instructions() -> str:
    return (
        "Compact the conversation into a concise state handoff for the next Codex turn. "
        "Preserve the active task, user requirements, important file paths, commands already run, "
        "tool results, decisions, blockers, and the latest state. Omit filler and repeated text."
    )


async def _as_compact_response(response: web.StreamResponse, model: str) -> web.Response:
    if not isinstance(response, web.Response) or response.status >= 400:
        return response
    try:
        payload = json.loads(response.text or "{}")
    except json.JSONDecodeError:
        return response
    output = payload.get("output") if isinstance(payload, dict) else None
    summary = _compact_summary_from_output(output)
    compacted = _compact_response_payload(model, summary, payload.get("usage") if isinstance(payload, dict) else None)
    return web.json_response(compacted)


def _compact_summary_from_output(output: Any) -> str:
    parts: list[str] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                content = item.get("content") or []
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("text"):
                            parts.append(str(part["text"]))
            elif item.get("type") == "output_text" and item.get("text"):
                parts.append(str(item["text"]))
    return "\n".join(part for part in parts if part).strip()


def _compact_response_payload(model: str, summary: str, usage: Any = None) -> dict[str, Any]:
    now = int(time.time())
    response_id = f"resp_compact_{now}"
    text = summary or "No prior conversation state was available to compact."
    payload = {
        "id": response_id,
        "object": "response",
        "created_at": now,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": f"msg_compact_{now}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


async def _error_response(upstream, *, slug: str | None = None) -> web.Response:
    text = await upstream.text()
    if slug:
        print(
            f"[err] upstream {slug} returned {upstream.status}: {text[:500]}",
            flush=True,
        )
    return web.Response(status=upstream.status, text=text, content_type=upstream.content_type or "text/plain")


async def _anthropic_error_response(upstream) -> web.Response:
    text = await upstream.text()
    message = text
    error_type = "api_error"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or message)
            error_type = str(err.get("type") or error_type)
        elif payload.get("message"):
            message = str(payload["message"])
    status_type = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
    }.get(upstream.status)
    if status_type:
        error_type = status_type
    body = {
        "type": "error",
        "error": {"type": error_type, "message": message},
    }
    request_id = upstream.headers.get("request-id") or upstream.headers.get("x-request-id")
    if request_id:
        body["request_id"] = request_id
    return web.json_response(body, status=upstream.status)


def _missing_api_key_message(route: ShimModel) -> str:
    env_name = route.raw.get("api_key_env") or route.raw.get("apiKeyEnv")
    if env_name:
        return f"Model {route.slug} has no API key. Set {env_name} or add api_key/apiKey for this model."
    return f"Model {route.slug} has no API key. Add api_key/apiKey or api_key_env/apiKeyEnv for this model."


def _normalize_roles(messages: list[dict]) -> list[dict]:
    result = []
    for message in messages:
        if isinstance(message, dict):
            message = dict(message)
            if message.get("role") == "developer":
                message["role"] = "system"
        result.append(message)
    return result


def _dump_debug_request(slug: str, url: str, body: dict[str, Any]) -> None:
    """Best-effort dump of the last forwarded request body for debugging.

    Writes ``.codex-shim/last_request.json`` next to the rest of the runtime
    state (catalog, pid, log). Failures are silently swallowed — this is a
    debug aid, not a code path the request should depend on.
    """
    try:
        dump_path = DEBUG_DIR / "last_request.json"
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"slug": slug, "url": url, "body": body}
        full = json.dumps(payload, indent=2, default=str)
        if len(full) > 2_000_000:
            messages = body.get("messages") or []
            summary = {
                "slug": slug,
                "url": url,
                "_truncated": True,
                "_full_size": len(full),
                "message_count": len(messages),
                "tool_count": len(body.get("tools") or []),
                "last_3_messages": messages[-3:],
            }
            dump_path.write_text(json.dumps(summary, indent=2, default=str))
        else:
            dump_path.write_text(full)
    except OSError as exc:
        print(f"[debug] dump failed: {exc}", flush=True)


def _current_managed_model() -> str | None:
    """Return the first ``model = "..."`` value from ~/.codex/config.toml."""
    if not CODEX_CONFIG_PATH.exists():
        return None
    try:
        text = CODEX_CONFIG_PATH.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("model = "):
            return stripped.split("=", 1)[1].strip().strip('"')
    return None


_MODEL_LINE_RE = re.compile(r'(?m)^(\s*model\s*=\s*")[^"]*(")')
_PROVIDER_NAME_RE = re.compile(
    r'(\[model_providers\.' + re.escape(PROVIDER_NAME) + r'\][^\[]*?\n\s*name\s*=\s*")[^"]*(")',
    re.DOTALL,
)


def _set_active_model(slug: str, display_name: str | None = None) -> None:
    """Rewrite the active model + provider label in ~/.codex/config.toml."""
    if not CODEX_CONFIG_PATH.exists():
        return
    try:
        text = CODEX_CONFIG_PATH.read_text()
    except OSError:
        return
    text = _MODEL_LINE_RE.sub(rf'\g<1>{slug}\g<2>', text, count=1)
    if display_name:
        text = _PROVIDER_NAME_RE.sub(rf'\g<1>{display_name}\g<2>', text, count=1)
    try:
        CODEX_CONFIG_PATH.write_text(text)
    except OSError as exc:
        print(f"[switch] failed to write {CODEX_CONFIG_PATH}: {exc}", flush=True)
        return
    print(f"[switch] set active model to {slug} ({display_name})", flush=True)


def _restart_codex_app() -> None:
    """Quit and relaunch Codex Desktop in a background thread (non-blocking).

    Cross-platform: ``taskkill`` + ``Codex.exe`` on Windows, ``osascript`` +
    ``open -a Codex`` on macOS. Linux has no Codex Desktop build today, so
    the branch is a no-op there.
    """
    import os as _os
    import subprocess as _subprocess
    import threading as _threading
    import time as _time

    def _do_restart() -> None:
        try:
            if _os.name == "nt":
                _subprocess.run(
                    ["taskkill", "/IM", "Codex.exe", "/F"],
                    check=False,
                    stdout=_subprocess.DEVNULL,
                    stderr=_subprocess.DEVNULL,
                )
                _time.sleep(1.5)
                local_appdata = _os.environ.get("LOCALAPPDATA", "")
                codex_exe = Path(local_appdata) / "Programs" / "Codex" / "Codex.exe"
                if codex_exe.exists():
                    _subprocess.Popen([str(codex_exe)])
                else:
                    _subprocess.Popen(["Codex.exe"], shell=True)
            elif sys.platform == "darwin":
                quit_script = 'tell application "Codex" to if it is running then quit'
                _subprocess.run(
                    ["osascript", "-e", quit_script],
                    check=False,
                    stdout=_subprocess.DEVNULL,
                    stderr=_subprocess.DEVNULL,
                )
                _time.sleep(1.5)
                _subprocess.Popen(["open", "-a", "Codex"])
        except OSError:
            pass

    _threading.Thread(target=_do_restart, daemon=True).start()


def _picker_html(picker_token: str) -> str:
    token_json = json.dumps(picker_token).replace("<", "\\u003c")
    html = '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex Shim - Model Picker</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0d1117; color: #c9d1d9;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh; padding: 20px;
  }
  .container { max-width: 500px; width: 100%; }
  h1 { font-size: 24px; margin-bottom: 8px; color: #f0f6fc; }
  .subtitle { color: #8b949e; margin-bottom: 24px; font-size: 14px; }
  .model-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; margin-bottom: 12px; cursor: pointer;
    transition: all 0.15s ease; display: flex; align-items: center;
    justify-content: space-between;
  }
  .model-card:hover { border-color: #58a6ff; background: #1c2333; }
  .model-card.active { border-color: #3fb950; background: #1a2e1a; }
  .model-info { flex: 1; }
  .model-name { font-size: 16px; font-weight: 600; color: #f0f6fc; }
  .model-provider { font-size: 12px; color: #8b949e; margin-top: 4px; }
  .model-badge {
    font-size: 11px; padding: 2px 8px; border-radius: 12px;
    font-weight: 600; text-transform: uppercase;
  }
  .badge-active { background: #1a4d2e; color: #3fb950; }
  .badge-switch { background: #1c2333; color: #58a6ff; }
  .status { text-align: center; margin-top: 16px; font-size: 14px; min-height: 20px; }
  .status.ok { color: #3fb950; }
  .status.err { color: #f85149; }
  .status.loading { color: #d29922; }
  .restart-note { color: #8b949e; font-size: 12px; text-align: center; margin-top: 8px; }
  .opt { display: flex; align-items: center; justify-content: center; gap: 8px; margin-top: 12px; }
  .opt label { font-size: 13px; color: #8b949e; cursor: pointer; }
  .opt input { cursor: pointer; }
</style>
</head>
<body>
<div class="container">
  <h1>Model Picker</h1>
  <p class="subtitle">Choose the active model for Codex Desktop</p>
  <div id="models"><div class="status loading">Loading models...</div></div>
  <div class="opt">
    <input type="checkbox" id="autoRestart" checked>
    <label for="autoRestart">Auto-restart Codex after switching</label>
  </div>
  <div id="status" class="status"></div>
  <p class="restart-note">Codex needs to restart to use the new model</p>
</div>
<script>
const PICKER_TOKEN = @@TOKEN_JSON@@;
async function loadModels() {
  const res = await fetch('/api/models');
  const models = await res.json();
  const container = document.getElementById('models');
  container.innerHTML = '';
  models.forEach(m => {
    const card = document.createElement('div');
    card.className = 'model-card' + (m.active ? ' active' : '');
    const info = document.createElement('div');
    info.className = 'model-info';
    const name = document.createElement('div');
    name.className = 'model-name';
    name.textContent = m.display_name;
    const prov = document.createElement('div');
    prov.className = 'model-provider';
    prov.textContent = m.provider + ' \u00b7 ' + m.slug;
    info.appendChild(name);
    info.appendChild(prov);
    const badge = document.createElement('span');
    badge.className = 'model-badge ' + (m.active ? 'badge-active' : 'badge-switch');
    badge.textContent = m.active ? 'Active' : 'Switch';
    card.appendChild(info);
    card.appendChild(badge);
    if (!m.active) {
      card.onclick = () => switchModel(m.slug);
    }
    container.appendChild(card);
  });
}
async function switchModel(slug) {
  const status = document.getElementById('status');
  const restart = document.getElementById('autoRestart').checked;
  status.className = 'status loading';
  status.textContent = 'Switching to ' + slug + '...';
  try {
    const res = await fetch('/api/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', '@@PICKER_HEADER@@': PICKER_TOKEN},
      body: JSON.stringify({slug, restart_codex: restart})
    });
    const data = await res.json();
    if (data.ok) {
      status.className = 'status ok';
      status.textContent = 'Switched to ' + slug + (restart ? ' \u2014 Codex restarting...' : '');
      setTimeout(loadModels, 1000);
    } else {
      status.className = 'status err';
      status.textContent = data.error || 'Failed';
    }
  } catch(e) {
    status.className = 'status err';
    status.textContent = 'Error: ' + e.message;
  }
}
loadModels();
</script>
</body>
</html>'''
    return (
        html.replace("@@TOKEN_JSON@@", token_json, 1).replace("@@PICKER_HEADER@@", PICKER_TOKEN_HEADER, 1)
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    shim = ShimServer(args.settings, host=args.host)
    web.run_app(shim.app(), host=args.host, port=args.port, handle_signals=True)


if __name__ == "__main__":
    main()
