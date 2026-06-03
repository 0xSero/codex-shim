"""End-to-end integration tests for the Auto Router against the real ShimServer.

These prove the router behaves correctly on the paths Codex actually uses:
streaming, compaction, the chat endpoint, the agent tool-call loop (per-task
cache), an Anthropic-shaped classifier, the exact HTTP the shim sends to the
classifier, and the full set of failure/edge cases. Everything runs offline
against mock upstreams — no keys, no network.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_shim import router
from codex_shim import server as server_module
from codex_shim.server import ShimServer


@pytest.fixture(autouse=True)
def _reset_router_cache():
    router.reset_cache()
    yield
    router.reset_cache()


@pytest.fixture(autouse=True)
def auth_missing(monkeypatch, tmp_path):
    """Keep ChatGPT passthrough out of discovery so model lists are deterministic."""
    missing = tmp_path / "missing-auth.json"
    monkeypatch.setattr("codex_shim.settings.DEFAULT_CODEX_AUTH", missing)
    monkeypatch.setattr("codex_shim.server.DEFAULT_CODEX_AUTH", missing)


# ---------------------------------------------------------------------------
# A versatile mock upstream: OpenAI chat completions + Anthropic messages, with
# request capture, streaming, and a classifier that scores from the task text.
# ---------------------------------------------------------------------------
def _ci_get(headers: dict, name: str):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _user_text_from_chat(messages):
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _default_score(user_text: str) -> dict:
    t = user_text.lower()
    if any(k in t for k in ("refactor", "across", "debug", "architecture", "race condition")):
        return {"cheap": 0.40, "strong": 0.95}
    if any(k in t for k in ("crud", "endpoint", "api", "tests", "implement", "parser")):
        return {"cheap": 0.55, "strong": 0.95}
    return {"cheap": 0.92, "strong": 0.96}


async def make_upstream(state):
    state.setdefault("chat_requests", [])
    state.setdefault("anthropic_requests", [])
    state.setdefault("classifier_calls", 0)
    state.setdefault("last_backend", None)
    state.setdefault("last_scores", None)
    state.setdefault("classifier_models", {"classifier-real", "claude-classifier-real"})
    state.setdefault("score_fn", _default_score)
    state.setdefault("fixed_scores", None)

    def _scores(user_text):
        if state["fixed_scores"] is not None:
            return state["fixed_scores"]
        return state["score_fn"](user_text)

    async def chat(request):
        body = await request.json()
        state["chat_requests"].append({"body": body, "headers": dict(request.headers)})
        model = body.get("model")
        if model in state["classifier_models"]:
            state["classifier_calls"] += 1
            scores = _scores(_user_text_from_chat(body.get("messages", [])))
            state["last_scores"] = scores
            return web.json_response(
                {
                    "choices": [{"message": {"role": "assistant", "content": json.dumps({"scores": scores, "reasoning": "x"})}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                }
            )
        state["last_backend"] = model
        if body.get("stream"):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(("data: %s\n\n" % json.dumps({"choices": [{"delta": {"content": f"hi from {model}"}}]})).encode())
            await resp.write(
                ("data: %s\n\n" % json.dumps({"choices": [{"delta": {}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})).encode()
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp
        return web.json_response(
            {
                "choices": [{"message": {"role": "assistant", "content": f"handled by {model}"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }
        )

    async def messages(request):
        body = await request.json()
        state["anthropic_requests"].append({"body": body, "headers": dict(request.headers)})
        model = body.get("model")
        if model in state["classifier_models"]:
            state["classifier_calls"] += 1
            user_text = body.get("messages", [{}])[-1].get("content", "")
            if isinstance(user_text, list):
                user_text = " ".join(str(p.get("text", "")) for p in user_text if isinstance(p, dict))
            scores = _scores(str(user_text))
            state["last_scores"] = scores
            return web.json_response(
                {"content": [{"type": "text", "text": json.dumps({"scores": scores})}], "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 3}}
            )
        state["last_backend"] = model
        return web.json_response(
            {"content": [{"type": "text", "text": f"handled by {model}"}], "stop_reason": "end_turn", "usage": {"input_tokens": 2, "output_tokens": 1}}
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/v1/messages", messages)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _settings(
    tmp_path,
    upstream_v1,
    *,
    classifier="classifier",
    classifier_provider="openai",
    classifier_model="classifier-real",
    threshold=0.7,
    cache=True,
    enabled=True,
    strong_provider="openai",
    extra_models=None,
    candidates=None,
):
    models = [
        {"slug": "cheap", "model": "cheap-real", "display_name": "Cheap", "provider": "openai", "base_url": upstream_v1, "api_key": "ck"},
        {"slug": "strong", "model": "strong-real", "display_name": "Strong", "provider": strong_provider, "base_url": upstream_v1, "api_key": "sk"},
        {"slug": "classifier", "model": classifier_model, "display_name": "Classifier", "provider": classifier_provider, "base_url": upstream_v1, "api_key": "xk"},
    ]
    if extra_models:
        models.extend(extra_models)
    if candidates is None:
        candidates = [
            {"slug": "cheap", "cost": 1, "supports_images": False, "card": "cheap fast single-file edits"},
            {"slug": "strong", "cost": 5, "supports_images": True, "card": "frontier multi-file refactors, debugging, images"},
        ]
    settings = tmp_path / "models.json"
    settings.write_text(
        json.dumps(
            {
                "models": models,
                "router": {
                    "enabled": enabled,
                    "slug": "codex-auto",
                    "display_name": "Auto (smart routing)",
                    "classifier": classifier,
                    "threshold": threshold,
                    "default": "cheap",
                    "cache": cache,
                    "candidates": candidates,
                },
            }
        )
    )
    return settings


async def _shim(settings):
    client = TestClient(TestServer(ShimServer(settings).app()))
    await client.start_server()
    return client


def _sse_events(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        if not block.startswith("data:"):
            continue
        data = block.removeprefix("data:").strip()
        if data and data != "[DONE]":
            events.append(json.loads(data))
    return events


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
async def test_streaming_through_auto_router_completes_and_routes(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1"))))

    resp = await shim.post("/v1/responses", json={"model": "codex-auto", "input": "refactor across modules", "stream": True})
    assert resp.status == 200
    events = _sse_events(await resp.text())
    completed = [e for e in events if e.get("type") == "response.completed"]
    assert completed, "missing response.completed event"
    assert state["last_backend"] == "strong-real"
    assert completed[-1]["response"]["usage"]["total_tokens"] == 3

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------
async def test_compact_through_auto_router(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1"))))

    resp = await shim.post(
        "/v1/responses/compact",
        json={
            "model": "codex-auto",
            "input": [
                {"role": "user", "content": "implement the parser"},
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
            ],
            "stream": True,
        },
    )
    assert resp.status == 200
    payload = await resp.json()
    assert payload["status"] == "completed"
    # medium task -> strong; the compact request reaches the routed backend.
    assert state["last_backend"] == "strong-real"

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# Chat-completions endpoint
# ---------------------------------------------------------------------------
async def test_chat_completions_through_auto_router(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1"))))

    resp = await shim.post(
        "/v1/chat/completions",
        json={"model": "codex-auto", "messages": [{"role": "user", "content": "add a docstring"}]},
    )
    assert resp.status == 200
    payload = await resp.json()
    assert payload["choices"][0]["message"]["content"] == "handled by cheap-real"
    assert state["last_backend"] == "cheap-real"

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# The agent tool-loop: per-task cache means one classification across many turns
# ---------------------------------------------------------------------------
async def test_tool_loop_reuses_one_classification_for_a_task(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1")), cache=True))

    # Turn 1: the original ask.
    await shim.post("/v1/responses", json={"model": "codex-auto", "input": [{"role": "user", "content": "implement the parser"}]})
    backend_1 = state["last_backend"]

    # Turns 2 and 3: the same task continuing through tool-call round-trips.
    for _ in range(2):
        state["last_backend"] = None
        await shim.post(
            "/v1/responses",
            json={
                "model": "codex-auto",
                "input": [
                    {"role": "user", "content": "implement the parser"},
                    {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"},
                    {"type": "function_call_output", "call_id": "c1", "output": "ran tests"},
                ],
            },
        )
        assert state["last_backend"] == backend_1  # same backend each turn

    assert state["classifier_calls"] == 1  # classifier paid once for the whole task

    await shim.close()
    await upstream.close()


async def test_distinct_tasks_each_get_classified(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1")), cache=True))

    await shim.post("/v1/responses", json={"model": "codex-auto", "input": "add a docstring"})
    await shim.post("/v1/responses", json={"model": "codex-auto", "input": "refactor across the codebase"})
    assert state["classifier_calls"] == 2

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# Anthropic-shaped classifier
# ---------------------------------------------------------------------------
async def test_anthropic_classifier_scores_and_routes(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    settings = _settings(
        tmp_path,
        str(upstream.make_url("/v1")),
        classifier_provider="anthropic",
        classifier_model="claude-classifier-real",
    )
    shim = await _shim(settings)

    resp = await shim.post("/v1/responses", json={"model": "codex-auto", "input": "refactor across files"})
    assert resp.status == 200
    assert state["last_backend"] == "strong-real"
    # The classifier call went out as an Anthropic /v1/messages request.
    classifier_reqs = [r for r in state["anthropic_requests"] if r["body"].get("model") == "claude-classifier-real"]
    assert len(classifier_reqs) == 1
    assert _ci_get(classifier_reqs[0]["headers"], "x-api-key") == "xk"
    assert _ci_get(classifier_reqs[0]["headers"], "anthropic-version")

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# Anthropic-shaped candidate (the routed model itself speaks Anthropic)
# ---------------------------------------------------------------------------
async def test_anthropic_candidate_is_routable(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1")), strong_provider="anthropic"))

    resp = await shim.post("/v1/responses", json={"model": "codex-auto", "input": "debug the race condition"})
    assert resp.status == 200
    payload = await resp.json()
    assert state["last_backend"] == "strong-real"
    assert payload["output"][0]["content"][0]["text"] == "handled by strong-real"

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# The exact request the shim sends to the classifier
# ---------------------------------------------------------------------------
async def test_classifier_request_payload_and_headers(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1"))))

    await shim.post("/v1/responses", json={"model": "codex-auto", "input": "write CRUD endpoints"})

    classifier_reqs = [r for r in state["chat_requests"] if r["body"].get("model") == "classifier-real"]
    assert len(classifier_reqs) == 1
    req = classifier_reqs[0]
    body = req["body"]
    assert body["stream"] is False
    assert body["temperature"] == 0
    assert body["max_tokens"] == 600  # default
    assert _ci_get(req["headers"], "Authorization") == "Bearer xk"

    system = body["messages"][0]
    user = body["messages"][1]
    assert system["role"] == "system"
    assert "task-routing classifier" in system["content"]
    # Capability cards + candidate slugs are handed to the classifier.
    assert "cheap fast single-file edits" in system["content"]
    assert "frontier multi-file refactors" in system["content"]
    assert "slug: cheap" in system["content"] and "slug: strong" in system["content"]
    assert user["role"] == "user"
    assert "write CRUD endpoints" in user["content"]

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# No overhead for normal (non-auto) requests
# ---------------------------------------------------------------------------
async def test_non_auto_request_never_calls_classifier(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1"))))

    resp = await shim.post("/v1/responses", json={"model": "cheap", "input": "anything"})
    assert resp.status == 200
    assert state["classifier_calls"] == 0
    assert state["last_backend"] == "cheap-real"

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# Threshold flows through from config
# ---------------------------------------------------------------------------
async def test_low_threshold_keeps_cheap(tmp_path):
    state = {"fixed_scores": {"cheap": 0.65, "strong": 0.95}}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1")), threshold=0.6, cache=False))

    await shim.post("/v1/responses", json={"model": "codex-auto", "input": "task"})
    assert state["last_backend"] == "cheap-real"  # 0.65 >= 0.6, cheapest wins

    await shim.close()
    await upstream.close()


async def test_high_threshold_escalates_to_strong(tmp_path):
    state = {"fixed_scores": {"cheap": 0.65, "strong": 0.95}}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1")), threshold=0.9, cache=False))

    await shim.post("/v1/responses", json={"model": "codex-auto", "input": "task"})
    assert state["last_backend"] == "strong-real"  # only 0.95 clears 0.9

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# Robust to bad classifier output
# ---------------------------------------------------------------------------
async def test_garbage_classifier_output_falls_back_to_default(tmp_path):
    # Classifier returns prose with no JSON -> empty scores -> fallback to default ("cheap").
    state = {"backend": None, "calls": 0}

    async def chat(request):
        body = await request.json()
        if body.get("model") == "classifier-real":
            state["calls"] += 1
            return web.json_response({"choices": [{"message": {"content": "I cannot help with that."}}], "usage": {}})
        state["backend"] = body.get("model")
        return web.json_response({"choices": [{"message": {"content": f"handled by {body.get('model')}"}}], "usage": {}})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    upstream = TestClient(TestServer(app))
    await upstream.start_server()

    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1")), cache=False))
    resp = await shim.post("/v1/responses", json={"model": "codex-auto", "input": "refactor across files"})
    assert resp.status == 200
    assert state["calls"] == 1  # classifier was consulted
    assert state["backend"] == "cheap-real"  # but its garbage reply fell back to default

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# Discovery gating
# ---------------------------------------------------------------------------
async def test_health_count_excludes_virtual_auto_model(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1"))))

    body = await (await shim.get("/health")).json()
    assert body["auto_router"] is True
    assert body["models"] == 3  # cheap + strong + classifier; NOT the virtual auto entry

    await shim.close()
    await upstream.close()


async def test_disable_env_removes_auto_from_discovery(tmp_path, monkeypatch):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1"))))
    monkeypatch.setenv("CODEX_SHIM_DISABLE_ROUTER", "1")

    models = await (await shim.get("/v1/models")).json()
    assert "codex-auto" not in [m["id"] for m in models["data"]]
    health = await (await shim.get("/health")).json()
    assert health["auto_router"] is False

    await shim.close()
    await upstream.close()


async def test_unavailable_candidates_hide_auto_entry(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    # Candidates reference slugs that have no usable backend.
    settings = _settings(
        tmp_path,
        str(upstream.make_url("/v1")),
        candidates=[
            {"slug": "ghost-a", "cost": 1, "card": "x"},
            {"slug": "ghost-b", "cost": 5, "card": "y"},
        ],
    )
    shim = await _shim(settings)

    models = await (await shim.get("/v1/models")).json()
    assert "codex-auto" not in [m["id"] for m in models["data"]]
    health = await (await shim.get("/health")).json()
    assert health["auto_router"] is False

    await shim.close()
    await upstream.close()


async def test_candidate_without_credentials_is_skipped(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    # "expensive" has no api_key -> unusable -> must never be selected even though
    # the classifier would score it highest.
    settings = _settings(
        tmp_path,
        str(upstream.make_url("/v1")),
        extra_models=[{"slug": "expensive", "model": "expensive-real", "display_name": "Expensive", "provider": "openai", "base_url": str(upstream.make_url("/v1"))}],
        candidates=[
            {"slug": "cheap", "cost": 1, "card": "cheap"},
            {"slug": "expensive", "cost": 99, "card": "best of all"},
        ],
    )
    state["score_fn"] = lambda _t: {"cheap": 0.8, "expensive": 0.99}
    shim = await _shim(settings)

    resp = await shim.post("/v1/responses", json={"model": "codex-auto", "input": "task"})
    assert resp.status == 200
    assert state["last_backend"] == "cheap-real"  # expensive dropped (no key)

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# Picker switch accepts the virtual model
# ---------------------------------------------------------------------------
async def test_switch_model_accepts_auto_slug(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(server_module, "_set_active_model", lambda slug, display=None: captured.update({"slug": slug, "display": display}))
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1"))))

    resp = await shim.post("/api/switch", json={"slug": "codex-auto", "restart_codex": False})
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True and data["model"] == "codex-auto"
    assert captured["slug"] == "codex-auto"
    assert captured["display"] == "Auto (smart routing)"

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# Score parsing robustness (unit) — prose, code fences, partial scores
# ---------------------------------------------------------------------------
def test_parse_scores_handles_code_fence():
    text = "```json\n{\"scores\": {\"cheap\": 0.8, \"strong\": 0.9}}\n```"
    assert router.parse_scores(text, ["cheap", "strong"]) == {"cheap": 0.8, "strong": 0.9}


def test_parse_scores_handles_prose_around_json():
    text = 'Here are my scores: {"scores": {"cheap": 0.3, "strong": 0.95}} — hope that helps!'
    assert router.parse_scores(text, ["cheap", "strong"]) == {"cheap": 0.3, "strong": 0.95}


def test_parse_scores_defaults_missing_candidate_to_zero():
    text = '{"scores": {"cheap": 0.9}}'
    assert router.parse_scores(text, ["cheap", "strong"]) == {"cheap": 0.9, "strong": 0.0}


# ---------------------------------------------------------------------------
# Concurrency: many simultaneous requests route correctly (shared cache safety)
# ---------------------------------------------------------------------------
async def test_concurrent_requests_route_correctly(tmp_path):
    state = {}
    upstream = await make_upstream(state)
    shim = await _shim(_settings(tmp_path, str(upstream.make_url("/v1")), cache=True))

    tasks = [("add a docstring", "cheap-real"), ("refactor across the whole codebase", "strong-real")] * 8

    async def fire(prompt):
        resp = await shim.post("/v1/responses", json={"model": "codex-auto", "input": prompt})
        payload = await resp.json()
        return resp.status, payload["output"][0]["content"][0]["text"]

    results = await asyncio.gather(*(fire(p) for p, _ in tasks))

    # Each response carries its own routed backend, independent of shared state.
    for (_prompt, expected), (status, text) in zip(tasks, results):
        assert status == 200
        assert text == f"handled by {expected}"

    await shim.close()
    await upstream.close()
