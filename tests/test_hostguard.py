from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from codex_shim.hostguard import build_allowed_hosts, host_only
from codex_shim.server import ShimServer


@pytest.mark.parametrize(
    "header,expected",
    [
        ("127.0.0.1:8765", "127.0.0.1"),
        ("localhost:8765", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
        ("[::1]:8765", "::1"),
        ("[::1]", "::1"),
        ("attacker.example:8765", "attacker.example"),
        ("attacker.example", "attacker.example"),
        ("", ""),
        ("  127.0.0.1:8765  ", "127.0.0.1"),
    ],
)
def test_host_only_strips_port(header, expected):
    assert host_only(header) == expected


def test_build_allowed_hosts_defaults_to_loopback():
    assert build_allowed_hosts("127.0.0.1") == {"127.0.0.1", "localhost", "::1"}


def test_build_allowed_hosts_adds_bind_host_and_env(monkeypatch):
    monkeypatch.setenv("CODEX_SHIM_ALLOWED_HOSTS", "shim.lan , Extra.Host")
    allowed = build_allowed_hosts("192.168.1.5")
    assert {"127.0.0.1", "localhost", "::1", "192.168.1.5", "shim.lan", "extra.host"} <= allowed


def test_build_allowed_hosts_ignores_wildcard_bind():
    assert build_allowed_hosts("0.0.0.0") == {"127.0.0.1", "localhost", "::1"}


@pytest.fixture
def settings_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "customModels": [
                    {
                        "model": "real-openai",
                        "displayName": "Real OpenAI",
                        "provider": "openai",
                        "baseUrl": "http://example.invalid/v1",
                        "apiKey": "secret",
                    }
                ]
            }
        )
    )
    return path


async def test_loopback_host_is_allowed(settings_file):
    client = TestClient(TestServer(ShimServer(settings_file).app()))
    await client.start_server()
    try:
        resp = await client.get("/health")
        assert resp.status == 200
    finally:
        await client.close()


async def test_rebinding_host_is_rejected_on_health(settings_file):
    client = TestClient(TestServer(ShimServer(settings_file).app()))
    await client.start_server()
    try:
        resp = await client.get("/health", headers={"Host": "attacker.example"})
        assert resp.status == 403
    finally:
        await client.close()


async def test_rebinding_host_is_rejected_before_upstream_forwarding(settings_file):
    # A POST with an attacker Host must be refused before the shim forwards the
    # request upstream with the user's credentials. example.invalid would fail
    # to resolve if we ever reached the upstream call, so a 403 proves the guard
    # short-circuits first.
    client = TestClient(TestServer(ShimServer(settings_file).app()))
    await client.start_server()
    try:
        resp = await client.post(
            "/v1/responses",
            json={"model": "real-openai", "input": "hi"},
            headers={"Host": "attacker.example"},
        )
        assert resp.status == 403
    finally:
        await client.close()


async def test_env_allowlisted_host_is_allowed(monkeypatch, settings_file):
    monkeypatch.setenv("CODEX_SHIM_ALLOWED_HOSTS", "shim.local")
    client = TestClient(TestServer(ShimServer(settings_file).app()))
    await client.start_server()
    try:
        resp = await client.get("/health", headers={"Host": "shim.local"})
        assert resp.status == 200
    finally:
        await client.close()
