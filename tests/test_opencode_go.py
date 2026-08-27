"""Tests for the OpenCode Go on-ramp HTTP paths (#47).

These cover the network-facing functions (`_request_json`, model discovery,
probes, and `refresh_opencode_go_settings`) by stubbing `urlopen`, since the
real endpoints require a paid API key and live network.
"""
from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from codex_shim import opencode_go


class _FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, handler):
    """handler(request) -> _FakeResponse | raises HTTPError/URLError."""
    captured = []

    def fake_urlopen(request, timeout=None):
        captured.append((request, timeout))
        return handler(request)

    monkeypatch.setattr(opencode_go, "urlopen", fake_urlopen)
    return captured


# ---------------------------------------------------------------------------
# _request_json
# ---------------------------------------------------------------------------
def test_request_json_parses_success(monkeypatch):
    _patch_urlopen(monkeypatch, lambda _req: _FakeResponse(200, json.dumps({"ok": True})))
    status, payload = opencode_go._request_json("GET", "https://x/models", {"Authorization": "Bearer k"})
    assert status == 200
    assert payload == {"ok": True}


def test_request_json_returns_http_error_status_and_body(monkeypatch):
    def handler(_req):
        raise HTTPError(
            url="https://x/models",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"error": "bad key"}).encode("utf-8")),
        )

    _patch_urlopen(monkeypatch, handler)
    status, payload = opencode_go._request_json("GET", "https://x/models", {})
    assert status == 401
    assert payload == {"error": "bad key"}


def test_request_json_url_error_raises_runtime(monkeypatch):
    def handler(_req):
        raise URLError("connection refused")

    _patch_urlopen(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="Could not reach OpenCode Go"):
        opencode_go._request_json("GET", "https://x/models", {})


def test_request_json_non_json_body_returns_empty_dict(monkeypatch):
    _patch_urlopen(monkeypatch, lambda _req: _FakeResponse(200, "<html>not json</html>"))
    status, payload = opencode_go._request_json("GET", "https://x/models", {})
    assert status == 200
    assert payload == {}


def test_request_json_sends_method_headers_and_body(monkeypatch):
    captured = _patch_urlopen(monkeypatch, lambda _req: _FakeResponse(200, "{}"))
    opencode_go._request_json(
        "POST",
        "https://x/chat/completions",
        {"Authorization": "Bearer k", "Content-Type": "application/json"},
        {"model": "m", "messages": []},
        timeout=12.0,
    )
    request, timeout = captured[0]
    assert request.get_method() == "POST"
    assert request.data == json.dumps({"model": "m", "messages": []}).encode("utf-8")
    assert request.headers["Authorization"] == "Bearer k"
    # User-Agent/Accept defaults are merged in.
    assert request.headers["User-agent"] == "codex-shim"
    assert timeout == 12.0


# ---------------------------------------------------------------------------
# fetch_opencode_go_model_ids
# ---------------------------------------------------------------------------
def test_fetch_model_ids_success(monkeypatch):
    payload = {"data": [{"id": "glm-5.1"}, {"id": "qwen3.7-max"}, {"id": ""}, {"no_id": 1}]}
    _patch_urlopen(monkeypatch, lambda _req: _FakeResponse(200, json.dumps(payload)))
    ids = opencode_go.fetch_opencode_go_model_ids("https://x/v1", "key")
    assert ids == ["glm-5.1", "qwen3.7-max"]


def test_fetch_model_ids_http_error_raises(monkeypatch):
    def handler(_req):
        raise HTTPError("https://x/v1/models", 403, "Forbidden", None, io.BytesIO(b"{}"))

    _patch_urlopen(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="returned HTTP 403"):
        opencode_go.fetch_opencode_go_model_ids("https://x/v1", "key")


def test_fetch_model_ids_missing_list_raises(monkeypatch):
    _patch_urlopen(monkeypatch, lambda _req: _FakeResponse(200, json.dumps({"data": "nope"})))
    with pytest.raises(RuntimeError, match="did not return a model list"):
        opencode_go.fetch_opencode_go_model_ids("https://x/v1", "key")


# ---------------------------------------------------------------------------
# probe_chat_model / probe_messages_model
# ---------------------------------------------------------------------------
def test_probe_chat_model_returns_status(monkeypatch):
    captured = _patch_urlopen(monkeypatch, lambda _req: _FakeResponse(200, "{}"))
    assert opencode_go.probe_chat_model("https://x/v1", "key", "glm-5.1") == 200
    request, _ = captured[0]
    assert request.full_url == "https://x/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer key"


def test_probe_messages_model_returns_status(monkeypatch):
    captured = _patch_urlopen(monkeypatch, lambda _req: _FakeResponse(404, "{}"))
    assert opencode_go.probe_messages_model("https://x/v1", "key", "claude") == 404
    request, _ = captured[0]
    assert request.full_url == "https://x/v1/messages"
    assert request.headers["X-api-key"] == "key"


# ---------------------------------------------------------------------------
# refresh_opencode_go_settings (full orchestration, stubbed network)
# ---------------------------------------------------------------------------
def test_refresh_requires_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Set OPENCODE_GO_API_KEY"):
        opencode_go.refresh_opencode_go_settings(tmp_path / "models.json")


def test_refresh_writes_models_and_records_skips(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "ocgo-secret")
    monkeypatch.setattr(
        opencode_go, "fetch_opencode_go_model_ids", lambda *_a, **_k: ["glm-5.1", "dead-model"]
    )

    def chat_status(_base, _key, model, **_k):
        return 200 if model == "glm-5.1" else 500

    def messages_status(_base, _key, model, **_k):
        return 500

    monkeypatch.setattr(opencode_go, "probe_chat_model", chat_status)
    monkeypatch.setattr(opencode_go, "probe_messages_model", messages_status)

    settings = tmp_path / "models.json"
    result = opencode_go.refresh_opencode_go_settings(settings)

    assert [row["model"] for row in result.models] == ["glm-5.1"]
    assert result.skipped == [("dead-model", 500, 500)]
    on_disk = json.loads(settings.read_text())
    assert [row["model"] for row in on_disk["models"]] == ["glm-5.1"]


# ---------------------------------------------------------------------------
# display_name_from_model_id
# ---------------------------------------------------------------------------
def test_display_name_from_model_id_aliases_and_titlecase():
    assert opencode_go.display_name_from_model_id("glm-5.1") == "GLM 5.1"
    assert opencode_go.display_name_from_model_id("kimi-k2") == "Kimi K2"
    assert opencode_go.display_name_from_model_id("deepseek-v4") == "DeepSeek V4"
    assert opencode_go.display_name_from_model_id("qwen3-max") == "Qwen3 Max"
