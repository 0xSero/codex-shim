from __future__ import annotations

import re
import subprocess
from pathlib import Path

from codex_shim.catalog import codex_config_overrides, write_config
from codex_shim.cli import (
    RUNTIME_DIR,
    exec_codex_app,
    patch_codex_app,
    restore_codex_app_bundle,
    start,
)
from codex_shim.settings import FactoryModel


def test_write_config_uses_posix_paths(tmp_path):
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
    assert "\\" not in config.read_text()


def test_codex_config_overrides_use_posix_paths():
    catalog = Path("C:\\Users\\test\\catalog.json")
    lines = codex_config_overrides(catalog, "kimi-k2-6", 8765)
    catalog_line = [ln for ln in lines if "model_catalog_json" in ln][0]
    assert "/" in catalog_line
    assert "C:\\\\Users\\\\test\\\\catalog.json" not in catalog_line


class TestPatchCodexApp:
    def test_returns_error_when_bundle_not_found(self, monkeypatch):
        monkeypatch.setattr(
            "codex_shim.cli._find_codex_app_bundle", lambda: (None, None)
        )
        assert patch_codex_app() == 1

    def test_returns_error_when_npx_missing(self, tmp_path, monkeypatch):
        fake_asar = tmp_path / "app.asar"
        fake_asar.write_bytes(b"fake")
        monkeypatch.setattr(
            "codex_shim.cli._find_codex_app_bundle",
            lambda: (fake_asar, tmp_path),
        )
        monkeypatch.setattr("codex_shim.cli._has_command", lambda _c: False)
        assert patch_codex_app() == 1

    def test_applies_picker_filter_and_copies_on_windows(self, tmp_path, monkeypatch):
        app_dir = tmp_path / "app"
        resources = app_dir / "resources"
        resources.mkdir(parents=True)
        asar = resources / "app.asar"
        asar.write_bytes(b"fake-asar")
        exe = app_dir / "Codex.exe"
        exe.write_bytes(b"prefix c7918ce4286488cdc1175e5e7a4aff32ade142c57a188dd74e136f328a5d34a7 suffix")

        workdir = tmp_path / "app-asar-work"
        assets = workdir / "webview" / "assets"
        assets.mkdir(parents=True)
        js = assets / "model-queries-abc123.js"
        js.write_text('let u=c.useHiddenModels&&o!==`amazonBedrock`,d;')

        monkeypatch.setattr(
            "codex_shim.cli._find_codex_app_bundle",
            lambda: (asar, app_dir),
        )
        monkeypatch.setattr("codex_shim.cli._has_command", lambda _c: True)

        calls = []

        def fake_subprocess_run(cmd, **kwargs):
            calls.append(cmd)
            if any("asar" in str(c) for c in cmd) and "extract" in cmd:
                import shutil

                dest = Path(cmd[-1])
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(workdir, dest)
            elif any("asar" in str(c) for c in cmd) and "pack" in cmd:
                Path(cmd[-1]).write_bytes(b"packed")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
        monkeypatch.setattr("codex_shim.cli._resign_codex_app", lambda: None)
        monkeypatch.setattr("codex_shim.cli._quit_codex_app", lambda: None)

        patched_root = tmp_path / "codex-desktop-patched"
        patched_app = patched_root / "app"
        monkeypatch.setattr("codex_shim.cli.IS_WINDOWS", True)
        monkeypatch.setattr("codex_shim.cli.PATCHED_CODEX_ROOT", patched_root)
        monkeypatch.setattr("codex_shim.cli.PATCHED_CODEX_APP_DIR", patched_app)

        assert patch_codex_app() == 0
        assert any("extract" in c for c in calls)
        assert any("pack" in c for c in calls)

        patched_js = RUNTIME_DIR / "app-asar-work" / "webview" / "assets" / "model-queries-abc123.js"
        assert "let u=!1,d;" in patched_js.read_text()

        copied_exe = patched_app / "Codex.exe"
        if copied_exe.exists():
            exe_content = copied_exe.read_bytes()
            assert b"c7918ce4286488cdc1175e5e7a4aff32ade142c57a188dd74e136f328a5d34a7" not in exe_content
            assert re.findall(rb"[a-f0-9]{64}", exe_content)


class TestWindowsAppLaunch:
    def test_exec_codex_app_launches_patched_exe(self, tmp_path, monkeypatch):
        patched_dir = tmp_path / "app"
        patched_dir.mkdir(parents=True)
        exe = patched_dir / "Codex.exe"
        exe.write_bytes(b"fake-exe")

        monkeypatch.setattr("codex_shim.cli.IS_WINDOWS", True)
        monkeypatch.setattr("codex_shim.cli.PATCHED_CODEX_APP_DIR", patched_dir)
        monkeypatch.setattr("codex_shim.cli._patched_codex_exe", lambda: exe)
        monkeypatch.setattr("codex_shim.cli._quit_codex_app", lambda: None)
        started = []

        def fake_startfile(path: str) -> None:
            started.append(path)

        monkeypatch.setattr("codex_shim.cli.os.startfile", fake_startfile)

        exec_codex_app(Path("settings.json"), 8765, ".")

        assert started == [str(exe)]

    def test_restore_removes_patched_copy(self, tmp_path, monkeypatch):
        monkeypatch.setattr("codex_shim.cli.RUNTIME_DIR", tmp_path)
        patched_root = tmp_path / "codex-desktop-patched"
        app_dir = patched_root / "app"
        app_dir.mkdir(parents=True)
        (app_dir / "Codex.exe").write_bytes(b"x")
        restored: list[str] = []

        monkeypatch.setattr("codex_shim.cli.IS_WINDOWS", True)
        monkeypatch.setattr("codex_shim.cli._quit_codex_app", lambda: None)
        monkeypatch.setattr("codex_shim.cli.time.sleep", lambda _s: None)
        monkeypatch.setattr(
            "codex_shim.cli.restore_codex_config",
            lambda: restored.append("config"),
        )

        assert restore_codex_app_bundle() == 0
        assert not patched_root.exists()
        assert restored == ["config"]


class TestMacOSRestoreUnchanged:
    def test_restore_writes_asar_on_non_windows(self, tmp_path, monkeypatch):
        monkeypatch.setattr("codex_shim.cli.IS_WINDOWS", False)
        app_asar = tmp_path / "app.asar"
        app_asar.write_bytes(b"patched")
        backup = RUNTIME_DIR / "app.asar.before-codex-shim-model-picker-patch"
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(b"original")
        monkeypatch.setattr(
            "codex_shim.cli._find_codex_app_bundle",
            lambda: (app_asar, tmp_path),
        )
        monkeypatch.setattr("codex_shim.cli._quit_codex_app", lambda: None)

        assert restore_codex_app_bundle() == 0
        assert app_asar.read_bytes() == b"original"


class TestWindowsDaemonStart:
    def test_start_fails_without_aiohttp(self, monkeypatch):
        monkeypatch.setattr("codex_shim.cli._read_pid", lambda: None)
        monkeypatch.setattr("codex_shim.cli._pid_running", lambda _pid: False)
        monkeypatch.setattr("codex_shim.cli._server_runtime_ready", lambda: False)
        assert start(Path("settings.json"), 8765) == 1

    def test_start_redirects_logs_and_waits_for_process(self, tmp_path, monkeypatch):
        log_path = tmp_path / "shim.log"
        monkeypatch.setattr("codex_shim.cli.LOG_PATH", log_path)

        class FakeProcess:
            pid = 4242

            def poll(self):
                return None

        popen_calls = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append(kwargs)
            return FakeProcess()

        monkeypatch.setattr("codex_shim.cli.IS_WINDOWS", True)
        monkeypatch.setattr("codex_shim.cli._read_pid", lambda: None)
        monkeypatch.setattr("codex_shim.cli._pid_running", lambda _pid: False)
        monkeypatch.setattr("codex_shim.cli._server_runtime_ready", lambda: True)
        monkeypatch.setattr("codex_shim.cli._healthy", lambda _port: True)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        assert start(Path("settings.json"), 8765) == 0
        assert popen_calls[0]["stdout"] is popen_calls[0]["stderr"]
