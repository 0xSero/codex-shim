from __future__ import annotations

import json

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
