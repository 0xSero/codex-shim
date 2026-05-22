from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_shim.catalog import codex_config_overrides, write_config
from codex_shim.cli import RUNTIME_DIR, patch_codex_app
from codex_shim.settings import FactoryModel


def test_write_config_uses_posix_paths(tmp_path):
    """Windows backslashes must be normalised to forward slashes in TOML."""
    catalog = tmp_path / "sub dir" / "custom_model_catalog.json"
    config = tmp_path / "config.toml"
    models = [
        FactoryModel(
            slug="kimi-k2-6",
            display_name="Kimi K2.6",
            model="kimi-k2-6",
            provider="openai",
            base_url="http://127.0.0.1:8765/v1",
            api_key="dummy",
            index=0,
        )
    ]
    write_config(models, config, catalog, port=8765)
    text = config.read_text()
    assert "model_catalog_json" in text
    # On Windows the path would contain backslashes; after as_posix() only / remain.
    # We verify no *unescaped* backslash survives (TOML would require \\\
    # but as_posix avoids that altogether).
    assert "\\" not in text


def test_codex_config_overrides_use_posix_paths():
    """codex_config_overrides must emit POSIX-style paths for TOML."""
    catalog = Path("C:\\Users\\test\\catalog.json")
    lines = codex_config_overrides(catalog, "kimi-k2-6", 8765)
    catalog_line = [ln for ln in lines if "model_catalog_json" in ln][0]
    assert "/" in catalog_line
    assert "C:\\\\Users\\\\test\\\\catalog.json" not in catalog_line


class TestPatchCodexApp:
    def test_returns_error_when_bundle_not_found(self, monkeypatch):
        """patch_codex_app must return 1 if Codex Desktop is not found."""
        monkeypatch.setattr(
            "codex_shim.cli._find_codex_app_bundle", lambda: (None, None)
        )
        assert patch_codex_app() == 1

    def test_returns_error_when_npx_missing(self, tmp_path, monkeypatch):
        """patch_codex_app must return 1 if npx is unavailable."""
        fake_asar = tmp_path / "app.asar"
        fake_asar.write_bytes(b"fake")
        monkeypatch.setattr(
            "codex_shim.cli._find_codex_app_bundle",
            lambda: (fake_asar, tmp_path),
        )
        monkeypatch.setattr("codex_shim.cli._has_command", lambda _c: False)
        assert patch_codex_app() == 1

    def test_applies_picker_filter_and_copies_on_windows(self, tmp_path, monkeypatch):
        """patch_codex_app extracts, patches JS, repacks, and copies the bundle."""
        # Build a fake Codex app tree
        app_dir = tmp_path / "app"
        resources = app_dir / "resources"
        resources.mkdir(parents=True)
        asar = resources / "app.asar"
        asar.write_bytes(b"fake-asar")
        exe = app_dir / "Codex.exe"
        exe.write_bytes(b"prefix c7918ce4286488cdc1175e5e7a4aff32ade142c57a188dd74e136f328a5d34a7 suffix")

        # Create a minimal asar workdir with the JS needle present
        workdir = tmp_path / "app-asar-work"
        assets = workdir / "webview" / "assets"
        assets.mkdir(parents=True)
        js = assets / "model-queries-abc123.js"
        js.write_text('let u=c.useHiddenModels&&o!==`amazonBedrock`,d;')

        # Mock _find_codex_app_bundle to point to our fake tree
        monkeypatch.setattr(
            "codex_shim.cli._find_codex_app_bundle",
            lambda: (asar, app_dir),
        )
        monkeypatch.setattr("codex_shim.cli._has_command", lambda _c: True)

        # Track subprocess calls so we can verify the asar pack happened
        calls = []

        def fake_subprocess_run(cmd, **kwargs):
            calls.append(cmd)
            if any("asar" in str(c) for c in cmd) and "extract" in cmd:
                # Simulate extraction by copying our pre-built workdir contents
                import shutil
                dest = Path(cmd[-1])
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(workdir, dest)
            elif any("asar" in str(c) for c in cmd) and "pack" in cmd:
                # Simulate pack by writing a dummy archive
                Path(cmd[-1]).write_bytes(b"packed")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

        # Prevent actual OS interaction for foreground / resign
        monkeypatch.setattr("codex_shim.cli._resign_codex_app", lambda: None)
        monkeypatch.setattr("codex_shim.cli._quit_codex_app", lambda: None)

        # On Windows the code copies the tree; on macOS it modifies in-place.
        # Force Windows path so we exercise the copy + integrity bypass logic.
        monkeypatch.setattr("codex_shim.cli.IS_WINDOWS", True)

        result = patch_codex_app()
        assert result == 0

        # Verify extraction and pack were both invoked
        assert any("extract" in c for c in calls)
        assert any("pack" in c for c in calls)

        # Verify the patched copy exists
        patched_dir = RUNTIME_DIR / "codex-desktop-patched" / "app"
        # The JS was patched inside the runtime workdir, not tmp_path
        patched_js = RUNTIME_DIR / "app-asar-work" / "webview" / "assets" / "model-queries-abc123.js"
        assert "let u=!1,d;" in patched_js.read_text()

        # Verify integrity bypass touched the copied exe
        copied_exe = RUNTIME_DIR / "codex-desktop-patched" / "app" / "Codex.exe"
        if copied_exe.exists():
            exe_content = copied_exe.read_bytes()
            # The old hash must be gone; some new hash (of the packed dummy asar) must be present
            assert b"c7918ce4286488cdc1175e5e7a4aff32ade142c57a188dd74e136f328a5d34a7" not in exe_content
            # Verify a 64-char hex string now lives where the old one was
            import re
            hashes = re.findall(rb"[a-f0-9]{64}", exe_content)
            assert len(hashes) >= 1
