from __future__ import annotations

import json

from codex_shim.catalog import catalog_entry, write_catalog, write_config
from codex_shim.settings import FactorySettings, chatgpt_passthrough_enabled, default_model_slug


def test_duplicate_models_get_unique_display_slugs(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "customModels": [
                    {"model": "gpt-5.5", "displayName": "Fast High", "provider": "openai", "baseUrl": "http://x/v1", "index": 1},
                    {"model": "gpt-5.5", "displayName": "Fast Low", "provider": "openai", "baseUrl": "http://x/v1", "index": 2},
                ]
            }
        )
    )
    models = FactorySettings(settings).load()
    assert [m.slug for m in models] == ["fast-high", "fast-low"]


def test_catalog_preserves_context_and_visibility():
    model = FactorySettingsFixture.one()
    entry = catalog_entry(model)
    assert entry["slug"] == "claude-opus"
    assert entry["visibility"] == "list"
    assert entry["context_window"] == 200000
    assert "free" in entry["available_in_plans"]


def test_chatgpt_passthrough_disabled_by_env(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "token"}}))
    monkeypatch.setenv("CODEX_SHIM_DISABLE_CHATGPT", "1")

    assert chatgpt_passthrough_enabled(auth) is False

    models = [FactorySettingsFixture.one()]
    catalog = tmp_path / "catalog.json"
    write_catalog(models, catalog)
    slugs = [row["slug"] for row in json.loads(catalog.read_text())["models"]]
    assert slugs == ["claude-opus"]


def test_default_model_uses_chatgpt_only_with_usable_auth(tmp_path, monkeypatch):
    model = FactorySettingsFixture.one()
    auth = tmp_path / "auth.json"

    assert chatgpt_passthrough_enabled(auth) is False
    assert default_model_slug([model], include_chatgpt=False) == "claude-opus"

    auth.write_text(json.dumps({"tokens": {"access_token": "token"}}))
    assert chatgpt_passthrough_enabled(auth) is True
    assert default_model_slug([model], include_chatgpt=True) == "gpt-5.5"

    catalog = tmp_path / "catalog.json"
    config = tmp_path / "config.toml"
    write_catalog([model], catalog, include_chatgpt=False)
    write_config([model], config, catalog, 8765, include_chatgpt=False)
    assert [row["slug"] for row in json.loads(catalog.read_text())["models"]] == ["claude-opus"]
    assert 'model = "claude-opus"' in config.read_text()


class FactorySettingsFixture:
    @staticmethod
    def one():
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "customModels": [
                        {
                            "model": "claude-opus",
                            "displayName": "Claude Opus",
                            "provider": "anthropic",
                            "baseUrl": "http://anthropic",
                            "maxContextLimit": 200000,
                        }
                    ]
                }
            )
        )
        return FactorySettings(path).load()[0]
