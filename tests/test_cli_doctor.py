from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codex_shim import cli


@pytest.fixture(autouse=True)
def doctor_safe_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CATALOG_PATH", tmp_path / "custom_model_catalog.json")
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(cli, "PID_PATH", tmp_path / "shim.pid")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "shim.log")
    monkeypatch.setattr(cli, "CODEX_CONFIG_PATH", tmp_path / "codex-config.toml")
    monkeypatch.setattr(cli, "DEFAULT_CODEX_AUTH", tmp_path / "auth.json")
    monkeypatch.setattr("codex_shim.settings.DEFAULT_CURSOR_API_KEY_FILE", tmp_path / "missing-cursor-api-key")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("CURSOR_AGENT_BIN", raising=False)
    monkeypatch.delenv("CODEX_SHIM_DISABLE_CHATGPT", raising=False)
    monkeypatch.delenv("CODEX_SHIM_DISABLE_CURSOR", raising=False)
    monkeypatch.delenv("CODEX_SHIM_DISABLE_ROUTER", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    monkeypatch.setattr(cli, "_health", lambda port: None)
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_pid_running", lambda pid: False)
    monkeypatch.setattr(
        cli,
        "available_model_slugs",
        lambda models: {model.slug for model in models if model.api_key.strip()},
    )
    monkeypatch.setattr(cli, "chatgpt_passthrough_available", lambda: False)
    monkeypatch.setattr(cli, "cursor_passthrough_available", lambda: False)


def _settings(path, models, router=None):
    data = {"models": models}
    if router is not None:
        data["router"] = router
    path.write_text(json.dumps(data))
    return path


def test_doctor_command_returns_zero_with_healthy_mocked_environment(monkeypatch, tmp_path, capsys):
    settings = _settings(
        tmp_path / "models.json",
        [
            {
                "model": "claude-upstream",
                "display_name": "Claude Upstream",
                "provider": "anthropic",
                "base_url": "https://example.invalid/v1",
                "api_key": "secret-anthropic",
            },
            {
                "model": "gpt-upstream",
                "display_name": "GPT Upstream",
                "provider": "generic-chat-completion-api",
                "base_url": "https://example.invalid/v1",
                "api_key": "secret-openai",
            },
        ],
        router={
            "enabled": True,
            "slug": "codex-auto",
            "classifier": "gpt-upstream",
            "candidates": [{"slug": "claude-upstream"}],
        },
    )
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda command: {"codex": "/usr/local/bin/codex", "cursor-agent": "/usr/local/bin/cursor-agent"}.get(command),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="codex-cli 0.133.0-alpha.1\n", stderr=""),
    )
    monkeypatch.setattr(cli, "_read_pid", lambda: 12345)
    monkeypatch.setattr(cli, "_pid_running", lambda pid: pid == 12345)
    monkeypatch.setattr(
        cli,
        "_health",
        lambda port: {
            "ok": True,
            "models": 4,
            "chatgpt_passthrough": True,
            "cursor_passthrough": True,
            "auto_router": True,
        },
    )
    monkeypatch.setattr(cli, "chatgpt_passthrough_available", lambda: True)
    monkeypatch.setattr(cli, "cursor_passthrough_available", lambda: True)

    code = cli.main(["--settings", str(settings), "--port", "8765", "doctor"])

    out = capsys.readouterr().out
    assert code == 0
    for section in (
        "Python",
        "Dependencies",
        "Codex CLI",
        "Settings",
        "Runtime files",
        "Shim daemon",
        "ChatGPT passthrough",
        "Cursor passthrough",
        "Proxy",
        "Codex config",
        "Summary",
    ):
        assert section in out
    assert "health ok: 4 models" in out
    assert "auto router active: codex-auto" in out
    assert "secret-anthropic" not in out
    assert "secret-openai" not in out


def test_invalid_settings_json_returns_one(tmp_path, capsys):
    settings = tmp_path / "broken.json"
    settings.write_text('{"models": [')

    code = cli.main(["--settings", str(settings), "doctor"])

    out = capsys.readouterr().out
    assert code == 1
    assert "invalid JSON" in out
    assert "Summary" in out


def test_missing_settings_file_warns_without_failing(tmp_path, capsys):
    settings = tmp_path / "missing.json"

    code = cli.main(["--settings", str(settings), "doctor"])

    out = capsys.readouterr().out
    assert code == 0
    assert "settings file not found" in out
    assert "Summary" in out


def test_missing_api_key_is_warn(tmp_path, capsys):
    settings = _settings(
        tmp_path / "models.json",
        [
            {
                "model": "missing-key-model",
                "display_name": "Missing Key Model",
                "provider": "generic-chat-completion-api",
                "base_url": "https://example.invalid/v1",
            }
        ],
    )

    code = cli.main(["--settings", str(settings), "doctor"])

    out = capsys.readouterr().out
    assert code == 0
    assert "WARN  models missing API keys: 1" in out


def test_no_proxy_complete_is_ok(monkeypatch, tmp_path, capsys):
    settings = _settings(tmp_path / "models.json", [])
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.delenv("no_proxy", raising=False)

    code = cli.main(["--settings", str(settings), "doctor"])

    out = capsys.readouterr().out
    assert code == 0
    assert "loopback hosts covered by NO_PROXY/no_proxy" in out
    assert "NO_PROXY/no_proxy does not include all loopback hosts" not in out


def test_no_proxy_missing_is_warn(monkeypatch, tmp_path, capsys):
    settings = _settings(tmp_path / "models.json", [])
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    code = cli.main(["--settings", str(settings), "doctor"])

    out = capsys.readouterr().out
    assert code == 0
    assert "NO_PROXY/no_proxy does not include all loopback hosts" in out


def test_daemon_health_output(monkeypatch, tmp_path, capsys):
    settings = _settings(tmp_path / "models.json", [])
    monkeypatch.setattr(
        cli,
        "_health",
        lambda port: {
            "ok": True,
            "models": 2,
            "chatgpt_passthrough": True,
            "cursor_passthrough": False,
            "auto_router": True,
        },
    )

    code = cli.main(["--settings", str(settings), "--port", "8765", "doctor"])

    out = capsys.readouterr().out
    assert code == 0
    assert "health ok: 2 models" in out
    assert "chatgpt_passthrough: true" in out
    assert "cursor_passthrough: false" in out
    assert "auto_router: true" in out


def test_codex_cli_not_found_is_warn_not_fail(monkeypatch, tmp_path, capsys):
    settings = _settings(tmp_path / "models.json", [])
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)

    code = cli.main(["--settings", str(settings), "doctor"])

    out = capsys.readouterr().out
    assert code == 0
    assert "codex not found on PATH" in out


def test_aiohttp_missing_is_fail(monkeypatch, tmp_path, capsys):
    settings = _settings(tmp_path / "models.json", [])
    monkeypatch.setattr(
        cli.importlib.util,
        "find_spec",
        lambda name: None if name == "aiohttp" else object(),
    )

    code = cli.main(["--settings", str(settings), "doctor"])

    out = capsys.readouterr().out
    assert code == 1
    assert "aiohttp is not importable" in out
    assert "FAIL" in out


def test_codex_config_installed_reports_provider_and_active_model(tmp_path, capsys):
    settings = _settings(tmp_path / "models.json", [])
    cli.CODEX_CONFIG_PATH.write_text(
        "\n".join(
            [
                cli.MANAGED_BEGIN,
                'model = "gpt-5.5"',
                'model_provider = "codex_shim"',
                cli.MANAGED_END,
                "[model_providers.codex_shim]",
                'name = "codex_shim"',
            ]
        )
    )

    code = cli.main(["--settings", str(settings), "doctor"])

    out = capsys.readouterr().out
    assert code == 0
    assert "shim provider configured" in out
    assert "active shim model: gpt-5.5" in out


def test_codex_config_uninstalled_is_info(tmp_path, capsys):
    settings = _settings(tmp_path / "models.json", [])

    code = cli.main(["--settings", str(settings), "doctor"])

    out = capsys.readouterr().out
    assert code == 0
    assert "shim provider is not currently installed" in out
