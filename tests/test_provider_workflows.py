from __future__ import annotations

import json
from pathlib import Path

from codex_shim import cli


def _spec(tmp_path, name="openrouter"):
    return cli.ProviderSpec(
        name=name,
        title="OpenRouter",
        settings_path=tmp_path / f"{name}-models.json",
        port=8766,
        placeholder_key="REPLACE_WITH_OPENROUTER_API_KEY",
        default_model="openai/gpt-4o-mini",
        default_display_name="OpenRouter GPT-4o Mini",
        default_provider="generic-chat-completion-api",
        default_base_url="https://openrouter.ai/api/v1",
        default_context=128000,
        allowed_providers=frozenset({"generic-chat-completion-api", "openai"}),
        template_path=Path("examples/openrouter-models.example.json"),
    )


def test_provider_setup_writes_private_settings_file(tmp_path, monkeypatch):
    spec = _spec(tmp_path)

    monkeypatch.setitem(cli.PROVIDER_SPECS, "test-openrouter", spec)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "sk-test")
    answers = iter(["anthropic/claude-sonnet-4", "Claude Sonnet", "https://openrouter.ai/api/v1", "131000"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    assert cli.setup_provider("test-openrouter") == 0

    payload = json.loads(spec.settings_path.read_text())
    row = payload["models"][0]
    assert row["api_key"] == "sk-test"
    assert row["model"] == "anthropic/claude-sonnet-4"
    assert row["provider"] == "generic-chat-completion-api"
    assert row["base_url"] == "https://openrouter.ai/api/v1"
    assert row["display_name"] == "Claude Sonnet"
    assert row["max_context_limit"] == 131000


def test_provider_setup_non_interactive_missing_settings_fails(tmp_path, monkeypatch):
    spec = _spec(tmp_path)

    monkeypatch.setitem(cli.PROVIDER_SPECS, "test-openrouter", spec)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    try:
        cli.setup_provider("test-openrouter")
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("setup should fail outside an interactive terminal")


def test_provider_run_uses_provider_port_inline_model_and_no_codex_config(tmp_path, monkeypatch):
    spec = _spec(tmp_path, name="minimax")
    spec.settings_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "model": "MiniMax-M2.7",
                        "provider": "generic-chat-completion-api",
                        "base_url": "https://api.minimax.io/v1",
                        "api_key": "secret",
                        "display_name": "MiniMax M2.7",
                    }
                ]
            }
        )
    )
    captured = {}
    codex_config = tmp_path / ".codex" / "config.toml"

    monkeypatch.delenv("CODEX_SHIM_DISABLE_CHATGPT", raising=False)
    monkeypatch.setitem(cli.PROVIDER_SPECS, "test-minimax", spec)
    monkeypatch.setattr(cli, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(cli, "CATALOG_PATH", tmp_path / "runtime" / "catalog.json")
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "runtime" / "config.toml")
    monkeypatch.setattr(cli, "CODEX_CONFIG_PATH", codex_config)
    monkeypatch.setattr(cli, "CODEX_CONFIG_BACKUP_PATH", tmp_path / "runtime" / "backup.toml")
    monkeypatch.setattr(cli, "ensure_started", lambda path, port: captured.update(started=(path, port)))
    monkeypatch.setattr(cli.os, "execvpe", lambda file, args, env: captured.update(file=file, args=args, env=env))

    assert cli.run_provider("test-minimax", ["hello"]) == 0

    assert captured["started"] == (spec.settings_path, 8766)
    assert captured["file"] == "codex"
    assert "-m" in captured["args"]
    assert "minimax-m2-7" in captured["args"]
    assert "hello" in captured["args"]
    assert captured["env"]["CODEX_SHIM_DISABLE_CHATGPT"] == "1"
    assert not codex_config.exists()


def test_provider_top_level_alias_runs_provider(tmp_path, monkeypatch):
    captured = {}

    monkeypatch.setitem(cli.PROVIDER_SPECS, "test-provider", _spec(tmp_path, name="test-provider"))
    monkeypatch.setattr(
        cli,
        "run_provider",
        lambda provider, args, port: captured.update(provider=provider, args=args, port=port) or 0,
    )

    assert cli.main(["--port", "9998", "test-provider", "."]) == 0

    assert captured == {"provider": "test-provider", "args": ["."], "port": 9998}
