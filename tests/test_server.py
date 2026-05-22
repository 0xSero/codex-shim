from __future__ import annotations

import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_shim.server import ShimServer


async def test_responses_routes_to_openai_chat(tmp_path):
    captured = {}

    async def chat(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.json()
        return web.json_response(
            {
                "id": "chatcmpl_fake",
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "usage": {"total_tokens": 3},
            }
        )

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", chat)
    upstream_client = TestClient(TestServer(upstream))
    await upstream_client.start_server()

    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "customModels": [
                    {
                        "model": "real-openai",
                        "displayName": "Real OpenAI",
                        "provider": "openai",
                        "baseUrl": str(upstream_client.make_url("/v1")),
                        "apiKey": "secret",
                    }
                ]
            }
        )
    )
    shim_client = TestClient(TestServer(ShimServer(settings).app()))
    await shim_client.start_server()

    resp = await shim_client.post("/v1/responses", json={"model": "real-openai", "input": "hi"})
    assert resp.status == 200
    payload = await resp.json()
    assert payload["output"][0]["content"][0]["text"] == "hello"
    assert captured["body"]["model"] == "real-openai"
    assert captured["headers"]["Authorization"] == "Bearer secret"

    await shim_client.close()
    await upstream_client.close()


async def test_minimax_provider_routes_to_openai_chat(tmp_path):
    captured = {}

    async def chat(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.json()
        return web.json_response(
            {
                "id": "chatcmpl_minimax",
                "choices": [{"message": {"role": "assistant", "content": "minimax hello"}}],
                "usage": {"total_tokens": 4},
            }
        )

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", chat)
    upstream_client = TestClient(TestServer(upstream))
    await upstream_client.start_server()

    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "customModels": [
                    {
                        "model": "MiniMax-M2.7",
                        "displayName": "MiniMax M2.7",
                        "provider": "minimax",
                        "baseUrl": str(upstream_client.make_url("/v1")),
                        "apiKey": "secret",
                    }
                ]
            }
        )
    )
    shim_client = TestClient(TestServer(ShimServer(settings).app()))
    await shim_client.start_server()

    resp = await shim_client.post("/v1/responses", json={"model": "minimax-m2-7", "input": "hi"})
    assert resp.status == 200
    payload = await resp.json()
    assert payload["output"][0]["content"][0]["text"] == "minimax hello"
    assert captured["body"]["model"] == "MiniMax-M2.7"
    assert captured["headers"]["Authorization"] == "Bearer secret"

    await shim_client.close()
    await upstream_client.close()


async def test_chat_routes_to_anthropic(tmp_path):
    captured = {}

    async def messages(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.json()
        return web.json_response({"id": "msg_fake", "content": [{"type": "text", "text": "anthropic hello"}], "stop_reason": "end_turn"})

    upstream = web.Application()
    upstream.router.add_post("/v1/messages", messages)
    upstream_client = TestClient(TestServer(upstream))
    await upstream_client.start_server()

    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "customModels": [
                    {
                        "model": "claude-real",
                        "displayName": "Claude Real",
                        "provider": "anthropic",
                        "baseUrl": str(upstream_client.make_url("")),
                        "apiKey": "secret",
                    }
                ]
            }
        )
    )
    shim_client = TestClient(TestServer(ShimServer(settings).app()))
    await shim_client.start_server()

    resp = await shim_client.post("/v1/chat/completions", json={"model": "claude-real", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status == 200
    payload = await resp.json()
    assert payload["choices"][0]["message"]["content"] == "anthropic hello"
    assert captured["body"]["model"] == "claude-real"
    assert captured["headers"]["x-api-key"] == "secret"

    await shim_client.close()
    await upstream_client.close()
