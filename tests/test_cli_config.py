from __future__ import annotations

import json
from pathlib import Path

from codex_shim import cli


def _settings(path, model: str = "claude-opus"):
    path.write_text(
        json.dumps(
            {
                "customModels": [
                    {
                        "model": model,
                        "displayName": "Claude Opus",
                        "provider": "anthropic",
                        "baseUrl": "http://anthropic",
                    }
                ]
            }
        )
    )
    return path


def _empty_settings(path):
    path.write_text(json.dumps({"customModels": []}))
    return path


def test_codex_wrapper_uses_inline_overrides_without_writing_codex_config(tmp_path, monkeypatch):
    settings = _settings(tmp_path / "settings.json")
    codex_config = tmp_path / "codex" / "config.toml"
    captured = {}

    monkeypatch.setenv("CODEX_SHIM_DISABLE_CHATGPT", "1")
    monkeypatch.setattr(cli, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(cli, "CATALOG_PATH", tmp_path / "runtime" / "catalog.json")
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "runtime" / "config.toml")
    monkeypatch.setattr(cli, "CODEX_CONFIG_PATH", codex_config)
    monkeypatch.setattr(cli, "CODEX_CONFIG_BACKUP_PATH", tmp_path / "runtime" / "backup.toml")
    monkeypatch.setattr(cli, "ensure_started", lambda *args: None)
    monkeypatch.setattr(cli.os, "execvp", lambda file, args: captured.update(file=file, args=args))

    rc = cli.main(["--settings", str(settings), "codex", "--", "--version"])

    assert rc == 0
    assert captured["file"] == "codex"
    assert captured["args"][0] == "codex"
    assert "-c" in captured["args"]
    assert "--version" in captured["args"]
    assert not codex_config.exists()


def test_current_model_reuse_requires_managed_valid_shim_model(tmp_path, monkeypatch):
    settings = _settings(tmp_path / "settings.json")
    models = cli.FactorySettings(settings).load()
    codex_config = tmp_path / "codex" / "config.toml"
    codex_config.parent.mkdir()
    codex_config.write_text('model = "unrelated-user-model"\n')

    monkeypatch.setenv("CODEX_SHIM_DISABLE_CHATGPT", "1")
    monkeypatch.setattr(cli, "CODEX_CONFIG_PATH", codex_config)
    assert cli._resolve_model_slug(models, None) == "claude-opus"

    top_block, _ = cli._managed_config_blocks("missing-old-shim-model", 8765)
    codex_config.write_text(top_block)
    assert cli._resolve_model_slug(models, None) == "claude-opus"

    top_block, _ = cli._managed_config_blocks("claude-opus", 8765)
    codex_config.write_text(top_block)
    assert cli._resolve_model_slug(models, None) == "claude-opus"


def test_disable_removes_managed_blocks_without_restoring_stale_backup(tmp_path, monkeypatch):
    codex_config = tmp_path / "codex" / "config.toml"
    backup = tmp_path / "runtime" / "config.toml.before-codex-shim"
    codex_config.parent.mkdir()
    backup.parent.mkdir()
    backup.write_text('model = "old-backup"\n')
    top_block, provider_block = cli._managed_config_blocks("claude-opus", 8765)
    codex_config.write_text(
        'model = "user-model"\n'
        'setting = true\n'
        '# user newer edit\n'
        + top_block
        + '\n[model_providers.factory_byok_shim]\nname = "old unmanaged provider"\n'
        + provider_block
    )

    monkeypatch.setattr(cli, "CODEX_CONFIG_PATH", codex_config)
    monkeypatch.setattr(cli, "CODEX_CONFIG_BACKUP_PATH", backup)

    cli.restore_codex_config()

    restored = codex_config.read_text()
    assert 'model = "old-backup"' not in restored
    assert 'model = "user-model"' in restored
    assert "setting = true" in restored
    assert "# user newer edit" in restored
    assert "factory_byok_shim" not in restored
    assert not backup.exists()


def test_install_then_disable_restores_original_top_level_keys_without_losing_newer_edits(tmp_path, monkeypatch):
    settings = _settings(tmp_path / "settings.json")
    codex_config = tmp_path / "codex" / "config.toml"
    backup = tmp_path / "runtime" / "config.toml.before-codex-shim"
    codex_config.parent.mkdir()
    codex_config.write_text(
        'model = "normal-model"\n'
        'model_provider = "normal_provider"\n'
        'model_catalog_json = "/tmp/normal-catalog.json"\n'
        'user_setting = true\n'
        '\n[model_providers.normal_provider]\n'
        'name = "Normal Provider"\n'
    )

    monkeypatch.setenv("CODEX_SHIM_DISABLE_CHATGPT", "1")
    monkeypatch.setattr(cli, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(cli, "CATALOG_PATH", tmp_path / "runtime" / "catalog.json")
    monkeypatch.setattr(cli, "CODEX_CONFIG_PATH", codex_config)
    monkeypatch.setattr(cli, "CODEX_CONFIG_BACKUP_PATH", backup)

    cli.install_codex_config(settings, 8765)
    installed = codex_config.read_text()
    assert 'model = "normal-model"' not in installed
    assert "codex-shim previous-top-level" in installed
    assert "[model_providers.factory_byok_shim]" in installed

    with codex_config.open("a") as f:
        f.write("\n# user newer edit while shim installed\n")

    cli.restore_codex_config()

    restored = codex_config.read_text()
    assert 'model = "normal-model"' in restored
    assert 'model_provider = "normal_provider"' in restored
    assert 'model_catalog_json = "/tmp/normal-catalog.json"' in restored
    assert "user_setting = true" in restored
    assert "[model_providers.normal_provider]" in restored
    assert "# user newer edit while shim installed" in restored
    assert "factory_byok_shim" not in restored
    assert "codex-shim previous-top-level" not in restored
    assert not backup.exists()


def test_generate_fails_when_no_factory_models_and_no_chatgpt_passthrough(tmp_path, monkeypatch):
    settings = _empty_settings(tmp_path / "settings.json")
    monkeypatch.setenv("CODEX_SHIM_DISABLE_CHATGPT", "1")
    monkeypatch.setattr(cli, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(cli, "CATALOG_PATH", tmp_path / "runtime" / "catalog.json")
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "runtime" / "config.toml")

    try:
        cli.generate(settings, 8765)
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError("generate should fail when no usable models exist")

    assert "No usable codex-shim models" in message
    assert not (tmp_path / "runtime" / "catalog.json").exists()
    assert not (tmp_path / "runtime" / "config.toml").exists()


def test_provider_setup_writes_ignored_local_settings(tmp_path, monkeypatch):
    settings = tmp_path / "runtime" / "openrouter-settings.json"
    spec = cli.ProviderSpec(
        name="openrouter",
        title="OpenRouter",
        settings_path=settings,
        port=8766,
        placeholder_key="REPLACE_WITH_OPENROUTER_API_KEY",
        default_model="openai/gpt-4o-mini",
        default_display_name="OpenRouter GPT-4o Mini",
        default_provider="generic-chat-completion-api",
        default_base_url="https://openrouter.ai/api/v1",
        default_context=128000,
        allowed_providers=frozenset({"generic-chat-completion-api", "openai"}),
        template_path=Path("examples/openrouter-settings.example.json"),
    )

    monkeypatch.setitem(cli.PROVIDER_SPECS, "test-openrouter", spec)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "sk-test")
    answers = iter(["anthropic/claude-sonnet-4", "Claude Sonnet", "https://openrouter.ai/api/v1", "131000"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    assert cli.setup_provider("test-openrouter") == 0

    payload = json.loads(settings.read_text())
    row = payload["customModels"][0]
    assert row["apiKey"] == "sk-test"
    assert row["model"] == "anthropic/claude-sonnet-4"
    assert row["provider"] == "generic-chat-completion-api"
    assert row["baseUrl"] == "https://openrouter.ai/api/v1"
    assert row["displayName"] == "Claude Sonnet"
    assert row["maxContextLimit"] == 131000


def test_provider_setup_non_interactive_missing_settings_fails(tmp_path, monkeypatch):
    spec = cli.ProviderSpec(
        name="minimax",
        title="MiniMax Token Plan",
        settings_path=tmp_path / "runtime" / "minimax-settings.json",
        port=8767,
        placeholder_key="REPLACE_WITH_MINIMAX_TOKEN_PLAN_KEY",
        default_model="MiniMax-M2.7",
        default_display_name="MiniMax M2.7",
        default_provider="minimax",
        default_base_url="https://api.minimax.io/v1",
        default_context=1000000,
        allowed_providers=frozenset({"minimax"}),
        template_path=Path("examples/minimax-token-plan-settings.example.json"),
    )

    monkeypatch.setitem(cli.PROVIDER_SPECS, "test-minimax", spec)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    try:
        cli.setup_provider("test-minimax")
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("setup should fail outside an interactive terminal")


def test_provider_run_uses_provider_settings_port_and_inline_model(tmp_path, monkeypatch):
    settings = tmp_path / "runtime" / "minimax-settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "customModels": [
                    {
                        "model": "MiniMax-M2.7",
                        "provider": "minimax",
                        "baseUrl": "https://api.minimax.io/v1",
                        "apiKey": "secret",
                        "displayName": "MiniMax M2.7",
                    }
                ]
            }
        )
    )
    spec = cli.ProviderSpec(
        name="minimax",
        title="MiniMax Token Plan",
        settings_path=settings,
        port=8767,
        placeholder_key="REPLACE_WITH_MINIMAX_TOKEN_PLAN_KEY",
        default_model="MiniMax-M2.7",
        default_display_name="MiniMax M2.7",
        default_provider="minimax",
        default_base_url="https://api.minimax.io/v1",
        default_context=1000000,
        allowed_providers=frozenset({"minimax"}),
        template_path=Path("examples/minimax-token-plan-settings.example.json"),
    )
    captured = {}

    monkeypatch.setenv("CODEX_SHIM_DISABLE_CHATGPT", "1")
    monkeypatch.setitem(cli.PROVIDER_SPECS, "test-minimax", spec)
    monkeypatch.setattr(cli, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(cli, "CATALOG_PATH", tmp_path / "runtime" / "catalog.json")
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "runtime" / "config.toml")
    monkeypatch.setattr(cli, "ensure_started", lambda path, port: captured.update(started=(path, port)))
    monkeypatch.setattr(cli.os, "execvp", lambda file, args: captured.update(file=file, args=args))

    rc = cli.run_provider("test-minimax", ["hello"])

    assert rc == 0
    assert captured["started"] == (settings, 8767)
    assert captured["file"] == "codex"
    assert "-m" in captured["args"]
    assert "minimax-m2-7" in captured["args"]
    assert "hello" in captured["args"]


def test_provider_top_level_alias_runs_provider(tmp_path, monkeypatch):
    captured = {}

    monkeypatch.setitem(
        cli.PROVIDER_SPECS,
        "test-provider",
        cli.ProviderSpec(
            name="test-provider",
            title="Test Provider",
            settings_path=tmp_path / "settings.json",
            port=9999,
            placeholder_key="PLACEHOLDER",
            default_model="model",
            default_display_name="Model",
            default_provider="openai",
            default_base_url="https://example.test/v1",
            default_context=1000,
            allowed_providers=frozenset({"openai"}),
            template_path=Path("examples/test.json"),
        ),
    )
    monkeypatch.setattr(cli, "run_provider", lambda provider, args, port: captured.update(provider=provider, args=args, port=port) or 0)

    rc = cli.main(["--port", "9998", "test-provider", "."])

    assert rc == 0
    assert captured == {"provider": "test-provider", "args": ["."], "port": 9998}
