from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_shim import router
from codex_shim.server import ShimServer
from codex_shim.catalog import write_catalog


@pytest.fixture(autouse=True)
def _reset_router_cache():
    router.reset_cache()
    yield
    router.reset_cache()


@pytest.fixture
def auth_missing(monkeypatch, tmp_path):
    """Keep ChatGPT passthrough out of discovery so model lists are deterministic."""
    missing = tmp_path / "missing-auth.json"
    monkeypatch.setattr("codex_shim.settings.DEFAULT_CODEX_AUTH", missing)
    monkeypatch.setattr("codex_shim.server.DEFAULT_CODEX_AUTH", missing)


CANDIDATES = [
    router.RouterCandidate(slug="cheap", cost=1.0, card="cheap fast model", supports_images=False),
    router.RouterCandidate(slug="strong", cost=5.0, card="frontier model", supports_images=True),
]


def _settings_with_router(tmp_path, upstream_v1, *, cache=False, enabled=True, classifier="classifier"):
    settings = tmp_path / "models.json"
    settings.write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "cheap", "model": "cheap-real", "display_name": "Cheap", "provider": "openai", "base_url": upstream_v1, "api_key": "k"},
                    {"slug": "strong", "model": "strong-real", "display_name": "Strong", "provider": "openai", "base_url": upstream_v1, "api_key": "k"},
                    {"slug": "classifier", "model": "classifier-real", "display_name": "Classifier", "provider": "openai", "base_url": upstream_v1, "api_key": "k"},
                ],
                "router": {
                    "enabled": enabled,
                    "slug": "codex-auto",
                    "display_name": "Auto (smart routing)",
                    "classifier": classifier,
                    "threshold": 0.7,
                    "default": "cheap",
                    "cache": cache,
                    "candidates": [
                        {"slug": "cheap", "cost": 1, "supports_images": False, "card": "cheap fast single-file edits"},
                        {"slug": "strong", "cost": 5, "supports_images": True, "card": "frontier multi-file refactors, debugging, images"},
                    ],
                },
            }
        )
    )
    return settings


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def test_load_router_config_parses_block(tmp_path):
    config = router.load_router_config(_settings_with_router(tmp_path, "http://x/v1"))
    assert config is not None
    assert config.enabled is True
    assert config.slug == "codex-auto"
    assert config.classifier == "classifier"
    assert config.threshold == 0.7
    assert [c.slug for c in config.candidates] == ["cheap", "strong"]
    assert config.candidates[1].supports_images is True


def test_load_router_config_absent_returns_none(tmp_path):
    settings = tmp_path / "models.json"
    settings.write_text(json.dumps({"models": []}))
    assert router.load_router_config(settings) is None
    assert router.load_router_config(tmp_path / "nope.json") is None


def test_disable_via_env_overrides_enabled(tmp_path, monkeypatch):
    config = router.load_router_config(_settings_with_router(tmp_path, "http://x/v1"))
    assert config.effective_enabled is True
    monkeypatch.setenv("CODEX_SHIM_DISABLE_ROUTER", "1")
    assert config.effective_enabled is False


def test_env_overrides_timeout_and_max_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SHIM_ROUTER_TIMEOUT", "3")
    monkeypatch.setenv("CODEX_SHIM_ROUTER_MAX_TOKENS", "42")
    config = router.load_router_config(_settings_with_router(tmp_path, "http://x/v1"))
    assert config.timeout == 3.0
    assert config.max_tokens == 42


# ---------------------------------------------------------------------------
# Task signal extraction
# ---------------------------------------------------------------------------
def test_latest_user_text_from_responses_input():
    body = {
        "input": [
            {"role": "user", "content": "first ask"},
            {"type": "function_call", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "done"},
        ]
    }
    assert router.latest_user_text(body) == "first ask"


def test_latest_user_text_from_chat_messages():
    body = {"messages": [{"role": "system", "content": "x"}, {"role": "user", "content": "hello there"}]}
    assert router.latest_user_text(body) == "hello there"


def test_has_images_detects_input_image():
    body = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "look"}, {"type": "input_image", "image_url": "data:..."}]}]}
    assert router.has_images(body) is True
    assert router.has_images({"input": "just text"}) is False


def test_task_signal_counts_tools_and_items():
    body = {"input": [{"role": "user", "content": "do it"}], "tools": [{"type": "function"}, {"type": "function"}]}
    signal = router.task_signal(body)
    assert signal["task"] == "do it"
    assert signal["tool_count"] == 2
    assert signal["input_items"] == 1
    assert signal["has_images"] is False


# ---------------------------------------------------------------------------
# Score parsing + selection
# ---------------------------------------------------------------------------
def test_parse_scores_pulls_json_object():
    text = 'Sure! {"scores": {"cheap": 0.4, "strong": 1.5}, "reasoning": "x"}'
    scores = router.parse_scores(text, ["cheap", "strong"])
    assert scores == {"cheap": 0.4, "strong": 1.0}  # clamped


def test_parse_scores_handles_garbage():
    assert router.parse_scores("no json here", ["cheap"]) == {}
    assert router.parse_scores("", ["cheap"]) == {}


def test_pick_candidate_prefers_cheapest_viable():
    scores = {"cheap": 0.9, "strong": 0.95}
    slug, score, why = router.pick_candidate(scores, CANDIDATES, 0.7, has_image_task=False)
    assert slug == "cheap"
    assert score == 0.9


def test_pick_candidate_escalates_when_cheap_below_bar():
    scores = {"cheap": 0.4, "strong": 0.95}
    slug, _score, _why = router.pick_candidate(scores, CANDIDATES, 0.7, has_image_task=False)
    assert slug == "strong"


def test_pick_candidate_hard_zeros_image_incapable():
    scores = {"cheap": 0.99, "strong": 0.8}
    slug, _score, _why = router.pick_candidate(scores, CANDIDATES, 0.7, has_image_task=True)
    assert slug == "strong"


def test_pick_candidate_below_bar_takes_best():
    scores = {"cheap": 0.5, "strong": 0.6}
    slug, _score, why = router.pick_candidate(scores, CANDIDATES, 0.7, has_image_task=False)
    assert slug == "strong"
    assert "below bar" in why


def test_fallback_slug_uses_default_then_cheapest():
    config = router.RouterConfig(
        enabled=True, slug="codex-auto", display_name="Auto", classifier=None,
        threshold=0.7, default="strong", cache=True, candidates=tuple(CANDIDATES), timeout=12.0, max_tokens=600,
    )
    assert router.fallback_slug(config, list(CANDIDATES)) == "strong"
    config_no_default = router.RouterConfig(
        enabled=True, slug="codex-auto", display_name="Auto", classifier=None,
        threshold=0.7, default=None, cache=True, candidates=tuple(CANDIDATES), timeout=12.0, max_tokens=600,
    )
    assert router.fallback_slug(config_no_default, list(CANDIDATES)) == "cheap"


def test_fallback_slug_skips_image_incapable_when_task_has_image():
    """When the task has images and the classifier-free fallback path runs
    (e.g. scoring returned all zeros, or the classifier was unreachable), the
    chosen fallback MUST be a candidate that can actually see images.

    Regression: a multi-modal task fell through to ``default = cheap`` (text-only),
    the request was forwarded to a vision-incapable upstream, and the upstream
    rejected the body with `unknown variant ``image_url`` `. The user-visible
    contract is "Auto Router never breaks a request"; sending an image task to
    a text-only model breaks that contract.
    """
    config = router.RouterConfig(
        enabled=True, slug="codex-auto", display_name="Auto", classifier=None,
        threshold=0.7,
        # default points at the text-only cheap model on purpose: that is the
        # configuration we ship with, so the fix must work for it.
        default="cheap",
        cache=True, candidates=tuple(CANDIDATES), timeout=12.0, max_tokens=600,
    )
    # Has-image=False: keep current behaviour (default wins).
    assert router.fallback_slug(config, list(CANDIDATES), has_image_task=False) == "cheap"
    # Has-image=True: must skip "cheap" (supports_images=False) and pick the
    # vision-capable candidate, even though it is not the configured default.
    assert router.fallback_slug(config, list(CANDIDATES), has_image_task=True) == "strong"


def test_fallback_slug_image_task_with_no_vision_candidate_returns_none():
    """If a task has images but no candidate supports them, fallback returns
    None rather than knowingly routing to a model that will reject the body.
    Returning None bubbles up to server.py which can surface the failure
    instead of producing a confusing 400 from the upstream.
    """
    text_only = [router.RouterCandidate(slug="only-text", cost=1.0, card="x", supports_images=False)]
    config = router.RouterConfig(
        enabled=True, slug="codex-auto", display_name="Auto", classifier=None,
        threshold=0.7, default="only-text", cache=True, candidates=tuple(text_only),
        timeout=12.0, max_tokens=600,
    )
    assert router.fallback_slug(config, text_only, has_image_task=True) is None


def test_fallback_slug_default_kwarg_preserves_old_call_sites():
    """``has_image_task`` must default to False so existing call sites and
    tests that don't pass the kwarg keep their previous behaviour."""
    config = router.RouterConfig(
        enabled=True, slug="codex-auto", display_name="Auto", classifier=None,
        threshold=0.7, default="strong", cache=True, candidates=tuple(CANDIDATES),
        timeout=12.0, max_tokens=600,
    )
    # No kwarg = old behaviour = honor default ("strong"), regardless of vision.
    assert router.fallback_slug(config, list(CANDIDATES)) == "strong"


# ---------------------------------------------------------------------------
# resolve_auto orchestration (with an injected fake classifier)
# ---------------------------------------------------------------------------
def _config(**overrides):
    base = dict(
        enabled=True, slug="codex-auto", display_name="Auto", classifier="classifier",
        threshold=0.7, default="cheap", cache=True, candidates=tuple(CANDIDATES), timeout=12.0, max_tokens=600,
    )
    base.update(overrides)
    return router.RouterConfig(**base)


async def test_resolve_single_candidate_skips_classifier():
    calls = []

    async def classify(_s, _u):
        calls.append(1)
        return "{}"

    slug, _info = await router.resolve_auto(_config(), [CANDIDATES[0]], {"input": "hi"}, classify)
    assert slug == "cheap"
    assert calls == []


async def test_resolve_no_classifier_is_deterministic():
    slug, info = await router.resolve_auto(_config(), list(CANDIDATES), {"input": "hi"}, None)
    assert slug == "cheap"  # cheapest fallback
    assert info["reason"] == "no classifier"


async def test_resolve_uses_classifier_scores():
    async def classify(_s, _u):
        return json.dumps({"scores": {"cheap": 0.4, "strong": 0.95}})

    slug, _info = await router.resolve_auto(_config(cache=False), list(CANDIDATES), {"input": "hard refactor"}, classify)
    assert slug == "strong"


async def test_resolve_caches_decision():
    calls = []

    async def classify(_s, _u):
        calls.append(1)
        return json.dumps({"scores": {"cheap": 0.9, "strong": 0.95}})

    body = {"input": "add a docstring"}
    first, _ = await router.resolve_auto(_config(cache=True), list(CANDIDATES), body, classify)
    second, info = await router.resolve_auto(_config(cache=True), list(CANDIDATES), body, classify)
    assert first == second == "cheap"
    assert len(calls) == 1  # second served from cache
    assert info["reason"] == "cache"


async def test_resolve_classifier_error_falls_back():
    async def classify(_s, _u):
        raise RuntimeError("boom")

    slug, info = await router.resolve_auto(_config(default="strong", cache=False), list(CANDIDATES), {"input": "x"}, classify)
    assert slug == "strong"
    assert info["reason"] == "classifier error"


# ---------------------------------------------------------------------------
# End-to-end through the real ShimServer
# ---------------------------------------------------------------------------
async def _make_upstream(captured):
    async def chat(request):
        body = await request.json()
        model = body.get("model")
        if model == "classifier-real":
            user = " ".join(
                m.get("content", "") for m in body.get("messages", []) if m.get("role") == "user" and isinstance(m.get("content"), str)
            )
            hard = any(k in user.lower() for k in ("refactor", "across", "debug"))
            scores = {"cheap": 0.4 if hard else 0.9, "strong": 0.95}
            return web.json_response(
                {"choices": [{"message": {"role": "assistant", "content": json.dumps({"scores": scores})}}], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
            )
        captured["backend"] = model
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": f"handled by {model}"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_auto_router_routes_trivial_to_cheap(tmp_path, auth_missing):
    captured = {}
    upstream = await _make_upstream(captured)
    settings = _settings_with_router(tmp_path, str(upstream.make_url("/v1")))
    shim = TestClient(TestServer(ShimServer(settings).app()))
    await shim.start_server()

    resp = await shim.post("/v1/responses", json={"model": "codex-auto", "input": "add a docstring to foo()"})
    assert resp.status == 200
    payload = await resp.json()
    assert payload["output"][0]["content"][0]["text"] == "handled by cheap-real"
    assert captured["backend"] == "cheap-real"

    await shim.close()
    await upstream.close()


async def test_auto_router_escalates_hard_task_to_strong(tmp_path, auth_missing):
    captured = {}
    upstream = await _make_upstream(captured)
    settings = _settings_with_router(tmp_path, str(upstream.make_url("/v1")))
    shim = TestClient(TestServer(ShimServer(settings).app()))
    await shim.start_server()

    resp = await shim.post(
        "/v1/responses",
        json={"model": "codex-auto", "input": "refactor the auth module across 8 files"},
    )
    assert resp.status == 200
    payload = await resp.json()
    assert captured["backend"] == "strong-real"
    assert payload["output"][0]["content"][0]["text"] == "handled by strong-real"

    await shim.close()
    await upstream.close()


async def test_auto_router_image_task_picks_image_capable(tmp_path, auth_missing):
    captured = {}
    upstream = await _make_upstream(captured)
    settings = _settings_with_router(tmp_path, str(upstream.make_url("/v1")))
    shim = TestClient(TestServer(ShimServer(settings).app()))
    await shim.start_server()

    resp = await shim.post(
        "/v1/responses",
        json={
            "model": "codex-auto",
            "input": [
                {"role": "user", "content": [
                    {"type": "input_text", "text": "what is in this screenshot?"},
                    {"type": "input_image", "image_url": "data:image/png;base64,xx"},
                ]}
            ],
        },
    )
    assert resp.status == 200
    # The cheap model scores high but can't see; only the image-capable model is viable.
    assert captured["backend"] == "strong-real"

    await shim.close()
    await upstream.close()


async def test_auto_router_falls_back_when_classifier_missing(tmp_path, auth_missing):
    captured = {}
    upstream = await _make_upstream(captured)
    # Point classifier at a slug that does not exist -> deterministic cheapest.
    settings = _settings_with_router(tmp_path, str(upstream.make_url("/v1")), classifier="nonexistent")
    shim = TestClient(TestServer(ShimServer(settings).app()))
    await shim.start_server()

    resp = await shim.post("/v1/responses", json={"model": "codex-auto", "input": "refactor across files"})
    assert resp.status == 200
    assert captured["backend"] == "cheap-real"  # cheapest, no scoring

    await shim.close()
    await upstream.close()


# ---------------------------------------------------------------------------
# Discovery: the virtual model shows up only when active
# ---------------------------------------------------------------------------
async def test_discovery_includes_auto_model(tmp_path, auth_missing):
    settings = _settings_with_router(tmp_path, "http://upstream.invalid/v1")
    server = ShimServer(settings)
    shim = TestClient(TestServer(server.app()))
    await shim.start_server()

    models = await (await shim.get("/v1/models")).json()
    assert "codex-auto" in [m["id"] for m in models["data"]]

    api = await (
        await shim.get("/api/models", headers={"X-Codex-Shim-Picker-Token": server.picker_token})
    ).json()
    auto = [m for m in api if m["slug"] == "codex-auto"]
    assert auto and auto[0]["provider"] == "auto"

    health = await (await shim.get("/health")).json()
    assert health["auto_router"] is True

    await shim.close()


async def test_disabled_router_not_in_discovery(tmp_path, auth_missing):
    settings = _settings_with_router(tmp_path, "http://upstream.invalid/v1", enabled=False)
    shim = TestClient(TestServer(ShimServer(settings).app()))
    await shim.start_server()

    models = await (await shim.get("/v1/models")).json()
    assert "codex-auto" not in [m["id"] for m in models["data"]]
    health = await (await shim.get("/health")).json()
    assert health["auto_router"] is False

    await shim.close()


def test_write_catalog_includes_auto_entry(tmp_path, auth_missing):
    from codex_shim.settings import ModelSettings

    settings = _settings_with_router(tmp_path, "http://upstream.invalid/v1")
    models = ModelSettings(settings).load()
    config = router.load_router_config(settings)
    catalog_path = tmp_path / "catalog.json"
    write_catalog(models, catalog_path, router_config=config)
    data = json.loads(catalog_path.read_text())
    slugs = [m["slug"] for m in data["models"]]
    assert slugs[0] == "codex-auto"


# ---------------------------------------------------------------------------
# RouterCache: bounded LRU eviction, per-instance isolation, invalidation (#44)
# ---------------------------------------------------------------------------
def test_router_cache_lru_evicts_oldest_not_bulk_clear():
    cache = router.RouterCache(maxsize=3)
    for i in range(3):
        cache.put(f"k{i}", f"v{i}")
    # Touch k0 so it becomes most-recently-used.
    assert cache.get("k0") == "v0"
    # Insert a 4th entry: the least-recently-used (k1) is evicted, others remain.
    cache.put("k3", "v3")
    assert len(cache) == 3  # bounded, NOT bulk-cleared to 0
    assert cache.get("k1") is None
    assert cache.get("k0") == "v0"
    assert cache.get("k2") == "v2"
    assert cache.get("k3") == "v3"


def test_router_cache_clear_empties_store():
    cache = router.RouterCache(maxsize=8)
    cache.put("a", "1")
    cache.put("b", "2")
    cache.clear()
    assert len(cache) == 0
    assert cache.get("a") is None


async def test_resolve_auto_uses_supplied_cache_instance():
    calls = []

    async def classify(_s, _u):
        calls.append(1)
        return json.dumps({"scores": {"cheap": 0.9, "strong": 0.95}})

    cache_a = router.RouterCache()
    cache_b = router.RouterCache()
    body = {"input": "add a docstring"}

    first, _ = await router.resolve_auto(_config(cache=True), list(CANDIDATES), body, classify, cache=cache_a)
    # Same body, different cache instance -> must re-run the classifier (no shared state).
    second, info = await router.resolve_auto(_config(cache=True), list(CANDIDATES), body, classify, cache=cache_b)
    assert first == second == "cheap"
    assert len(calls) == 2
    # Re-using cache_a serves from cache (no extra classifier call).
    third, info3 = await router.resolve_auto(_config(cache=True), list(CANDIDATES), body, classify, cache=cache_a)
    assert third == "cheap"
    assert len(calls) == 2
    assert info3["reason"] == "cache"


async def test_switch_model_clears_router_cache(tmp_path, auth_missing, monkeypatch):
    from codex_shim import server as server_module
    from codex_shim.server import PICKER_TOKEN_HEADER

    settings = _settings_with_router(tmp_path, "http://upstream.invalid/v1")
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "cheap-real"\n'
        'model_provider = "codex_shim"\n\n'
        '[model_providers.codex_shim]\n'
        'name = "Codex Shim"\n'
    )
    monkeypatch.setattr(server_module, "CODEX_CONFIG_PATH", config)
    monkeypatch.setattr(server_module, "_restart_codex_app", lambda: None)

    shim = ShimServer(settings)
    shim.router_cache.put("k0", "cheap")
    assert len(shim.router_cache) == 1

    client = TestClient(TestServer(shim.app()))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/switch",
            json={"slug": "strong", "restart_codex": False},
            headers={PICKER_TOKEN_HEADER: shim.picker_token},
        )
        assert resp.status == 200
        # The model switch must invalidate stale routing decisions.
        assert len(shim.router_cache) == 0
    finally:
        await client.close()
