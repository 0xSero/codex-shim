from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import hashlib
import re
from urllib.request import urlopen

IS_WINDOWS = sys.platform == "win32"

from .catalog import codex_config_overrides, write_catalog, write_config
from .settings import DEFAULT_FACTORY_SETTINGS, DEFAULT_HOST, DEFAULT_PORT, FactorySettings, default_model_slug


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_ROOT / ".codex-shim"
CATALOG_PATH = RUNTIME_DIR / "custom_model_catalog.json"
CONFIG_PATH = RUNTIME_DIR / "config.toml"
PID_PATH = RUNTIME_DIR / "shim.pid"
LOG_PATH = RUNTIME_DIR / "shim.log"
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
CODEX_CONFIG_BACKUP_PATH = RUNTIME_DIR / "config.toml.before-codex-shim"
MANAGED_BEGIN = "# >>> codex-shim managed >>>"
MANAGED_END = "# <<< codex-shim managed <<<"
PATCHED_CODEX_APP_DIR = RUNTIME_DIR / "codex-desktop-patched" / "app"
PATCHED_CODEX_ROOT = RUNTIME_DIR / "codex-desktop-patched"
PATCHED_CODEX_POINTER = RUNTIME_DIR / "patched-app-path.txt"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-shim")
    parser.add_argument("--settings", type=Path, default=DEFAULT_FACTORY_SETTINGS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate")
    sub.add_parser("list")
    sub.add_parser("start")
    sub.add_parser("enable")
    sub.add_parser("stop")
    sub.add_parser("disable")
    sub.add_parser("restart")
    sub.add_parser("status")
    sub.add_parser("patch-app", help="Patch Codex Desktop model dropdown to allow custom catalog models.")
    sub.add_parser("restore-app", help="Restore Codex Desktop app.asar from the pre-patch backup.")

    model_parser = sub.add_parser("model", help="List or set the active shim model in Codex config.")
    model_sub = model_parser.add_subparsers(dest="model_command", required=True)
    model_sub.add_parser("list")
    use_parser = model_sub.add_parser("use")
    use_parser.add_argument("model_slug")

    codex_parser = sub.add_parser("codex", help="Run Codex CLI with opt-in shim config overrides.")
    codex_parser.add_argument("args", nargs=argparse.REMAINDER)

    app_parser = sub.add_parser("app", help="Launch Codex Desktop with opt-in shim config overrides.")
    app_parser.add_argument("-m", "--model", dest="model_slug")
    app_parser.add_argument("path", nargs="?", default=".")

    args = parser.parse_args(argv)
    if args.command == "generate":
        generate(args.settings, args.port)
        return 0
    if args.command == "list":
        return list_models(args.settings)
    if args.command in {"start", "enable"}:
        generate(args.settings, args.port)
        code = start(args.settings, args.port)
        if code == 0 and args.command == "enable":
            install_codex_config(args.settings, args.port)
        return code
    if args.command in {"stop", "disable"}:
        if args.command == "disable":
            restore_codex_config()
        return stop()
    if args.command == "restart":
        stop()
        generate(args.settings, args.port)
        return start(args.settings, args.port)
    if args.command == "status":
        return status(args.port)
    if args.command == "patch-app":
        return patch_codex_app()
    if args.command == "restore-app":
        return restore_codex_app_bundle()
    if args.command == "model":
        if args.model_command == "list":
            return list_models(args.settings)
        if args.model_command == "use":
            generate(args.settings, args.port)
            ensure_started(args.settings, args.port)
            install_codex_config(args.settings, args.port, args.model_slug)
            print(f"Active Codex shim model: {args.model_slug}")
            return 0
    if args.command == "codex":
        generate(args.settings, args.port)
        ensure_started(args.settings, args.port)
        exec_codex(args.settings, args.port, args.args)
        return 0
    if args.command == "app":
        generate(args.settings, args.port)
        ensure_started(args.settings, args.port)
        install_codex_config(args.settings, args.port, args.model_slug)
        exec_codex_app(args.settings, args.port, args.path)
        return 0
    return 2


def generate(settings_path: Path, port: int) -> None:
    models = FactorySettings(settings_path).load()
    write_catalog(models, CATALOG_PATH)
    write_config(models, CONFIG_PATH, CATALOG_PATH, port)
    print(f"Generated {len(models)} model entries:")
    print(f"  catalog: {CATALOG_PATH}")
    print(f"  config:  {CONFIG_PATH}")
    print("No files under ~/.codex were modified.")


def install_codex_config(settings_path: Path, port: int, model_slug: str | None = None) -> None:
    models = FactorySettings(settings_path).load()
    default_slug = _resolve_model_slug(models, model_slug)
    CODEX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    original = CODEX_CONFIG_PATH.read_text() if CODEX_CONFIG_PATH.exists() else ""
    if MANAGED_BEGIN not in original and not CODEX_CONFIG_BACKUP_PATH.exists():
        CODEX_CONFIG_BACKUP_PATH.write_text(original)
    cleaned = _remove_managed_config(original)
    cleaned = _remove_top_level_keys(cleaned, {"model", "model_provider", "model_catalog_json"})
    cleaned = _remove_section(cleaned, "model_providers.factory_byok_shim")
    top_block, provider_block = _managed_config_blocks(default_slug, port)
    CODEX_CONFIG_PATH.write_text(top_block + "\n" + cleaned.lstrip() + "\n" + provider_block)
    print(f"Installed shim config into {CODEX_CONFIG_PATH}.")
    print(f"Original backup: {CODEX_CONFIG_BACKUP_PATH}")


def list_models(settings_path: Path) -> int:
    models = FactorySettings(settings_path).load()
    width = max([len(m.slug) for m in models] + [4])
    for model in models:
        print(f"{model.slug:<{width}}  {model.display_name}  ->  {model.model} ({model.provider})")
    return 0


def start(settings_path: Path, port: int) -> int:
    if _pid_running(_read_pid()):
        print(f"Shim already running with pid {_read_pid()}.")
        return 0
    if not _server_runtime_ready():
        return 1
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "codex_shim.server",
        "--settings",
        str(settings_path),
        "--host",
        DEFAULT_HOST,
        "--port",
        str(port),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    log = LOG_PATH.open("ab")
    popen_kwargs: dict = {
        "stdout": log,
        "stderr": log,
    }
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        popen_kwargs["close_fds"] = True
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env, stdin=subprocess.DEVNULL, **popen_kwargs)
    PID_PATH.write_text(str(process.pid))
    for _ in range(50):
        if process.poll() is not None:
            print(f"Shim exited during startup. See {LOG_PATH}.", file=sys.stderr)
            _print_log_tail()
            return 1
        if _healthy(port):
            print(f"Shim started on http://{DEFAULT_HOST}:{port} with pid {process.pid}.")
            print(f"Log: {LOG_PATH}")
            return 0
        time.sleep(0.1)
    print(f"Shim process started but health check timed out. See {LOG_PATH}.", file=sys.stderr)
    _print_log_tail()
    return 1


def stop() -> int:
    pid = _read_pid()
    if not _pid_running(pid):
        print("Shim is not running.")
        PID_PATH.unlink(missing_ok=True)
        return 0
    _terminate_pid(pid)
    for _ in range(50):
        if not _pid_running(pid):
            PID_PATH.unlink(missing_ok=True)
            print("Shim stopped.")
            return 0
        time.sleep(0.1)
    print(f"Shim pid {pid} did not exit after signal.", file=sys.stderr)
    return 1


def restore_codex_config() -> None:
    if CODEX_CONFIG_BACKUP_PATH.exists():
        CODEX_CONFIG_PATH.write_text(CODEX_CONFIG_BACKUP_PATH.read_text())
        CODEX_CONFIG_BACKUP_PATH.unlink()
        print(f"Restored original {CODEX_CONFIG_PATH}.")
        return
    if CODEX_CONFIG_PATH.exists():
        current = CODEX_CONFIG_PATH.read_text()
        restored = _remove_managed_config(current)
        restored = _remove_section(restored, "model_providers.factory_byok_shim")
        CODEX_CONFIG_PATH.write_text(restored.lstrip())
        print(f"Removed shim config from {CODEX_CONFIG_PATH}.")


def status(port: int) -> int:
    pid = _read_pid()
    healthy = _healthy(port)
    if _pid_running(pid) and not healthy:
        time.sleep(0.5)
        healthy = _healthy(port)
    if healthy and not _pid_running(pid):
        print(
            f"http://{DEFAULT_HOST}:{port}/health is up but shim pid {pid} is not running. "
            "Another process may own the port. Run: codex-shim stop",
            file=sys.stderr,
        )
        return 1
    if _pid_running(pid) and healthy:
        print(f"Shim is running on http://{DEFAULT_HOST}:{port} with pid {pid}.")
        return 0
    if _pid_running(pid):
        print(f"Shim process {pid} exists but health check failed. See {LOG_PATH}.", file=sys.stderr)
        _print_log_tail()
        return 1
    if healthy:
        print(f"Shim is stopped but http://{DEFAULT_HOST}:{port}/health responded.")
        return 1
    print("Shim is stopped.")
    return 1


def ensure_started(settings_path: Path, port: int) -> None:
    if not (_pid_running(_read_pid()) and _healthy(port)):
        code = start(settings_path, port)
        if code:
            raise SystemExit(code)


def exec_codex(settings_path: Path, port: int, codex_args: list[str]) -> None:
    overrides = _override_args(settings_path, port)
    codex_args = list(codex_args or [])
    if codex_args[:1] == ["--"]:
        codex_args = codex_args[1:]
    args = ["codex", *overrides, *codex_args]
    os.execvp("codex", args)


def exec_codex_app(settings_path: Path, port: int, path: str) -> None:
    _quit_codex_app()
    patched_exe = _patched_codex_exe()
    if IS_WINDOWS and patched_exe is not None:
        os.startfile(str(patched_exe))
        return
    if IS_WINDOWS:
        print(
            f"Patched Codex Desktop not found at {_active_patched_app_dir()}. "
            "Run: codex-shim patch-app",
            file=sys.stderr,
        )
    args = ["codex", "app", path]
    subprocess.Popen(args)
    _foreground_codex_app()


def _active_patched_app_dir() -> Path:
    if PATCHED_CODEX_POINTER.exists():
        recorded = Path(PATCHED_CODEX_POINTER.read_text().strip())
        if recorded.exists():
            return recorded
    return PATCHED_CODEX_APP_DIR


def _set_active_patched_app_dir(app_dir: Path) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PATCHED_CODEX_POINTER.write_text(str(app_dir.resolve()))


def _patched_codex_exe() -> Path | None:
    exe = _active_patched_app_dir() / "Codex.exe"
    return exe if exe.exists() else None


def _patched_install_roots() -> list[Path]:
    roots = sorted(RUNTIME_DIR.glob("codex-desktop-patched*"), key=lambda p: p.name)
    return [p for p in roots if p.is_dir()]


def _quit_codex_app() -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/IM", "Codex.exe", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for root in _patched_install_roots():
            _kill_windows_processes_under(root)
        time.sleep(1.0)
        return
    script = 'tell application "Codex" to if it is running then quit'
    try:
        subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
    except OSError:
        pass


def patch_codex_app() -> int:
    needle = "let u=c.useHiddenModels&&o!==`amazonBedrock`,d;"
    replacement = "let u=!1,d;"

    app_asar, app_dir = _find_codex_app_bundle()
    if app_asar is None or app_dir is None:
        print("Codex Desktop app bundle not found.", file=sys.stderr)
        return 1
    npx_cmd = "npx.cmd" if IS_WINDOWS else "npx"
    if not _has_command(npx_cmd):
        print("npx is required to patch the Electron asar bundle.", file=sys.stderr)
        return 1

    backup = RUNTIME_DIR / "app.asar.before-codex-shim-model-picker-patch"
    workdir = RUNTIME_DIR / "app-asar-work"

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        backup.write_bytes(app_asar.read_bytes())
        print(f"Backed up original app.asar to {backup}.")
    versioned_backup = RUNTIME_DIR / f"app.asar.before-codex-shim-model-picker-patch.{_app_asar_hash(app_asar)[:12]}"
    if not versioned_backup.exists():
        versioned_backup.write_bytes(app_asar.read_bytes())
        print(f"Backed up current app.asar to {versioned_backup}.")

    _quit_codex_app()
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    subprocess.run([npx_cmd, "--yes", "asar", "extract", str(app_asar), str(workdir)], check=True)
    bundle_file = _find_model_queries_bundle(workdir, needle, replacement)
    if bundle_file is None:
        print("Could not find the expected model picker filter in Codex Desktop.", file=sys.stderr)
        return 1
    text = bundle_file.read_text()
    changed = False
    if replacement in text:
        print("Codex Desktop model picker patch is already applied.")
    elif needle in text:
        bundle_file.write_text(text.replace(needle, replacement))
        changed = True
        print("Patched Codex Desktop model picker allowlist filter.")
    else:
        print("Could not find the expected model picker filter in Codex Desktop.", file=sys.stderr)
        return 1

    if IS_WINDOWS:
        patched_dir = _prepare_patched_app_dir(app_dir)
        patched_asar = patched_dir / "resources" / "app.asar"
        patched_exe = patched_dir / "Codex.exe"
        subprocess.run([npx_cmd, "--yes", "asar", "pack", str(workdir), str(patched_asar)], check=True)
        _patch_asar_integrity_bypass(patched_exe, _app_asar_hash(app_asar), _app_asar_hash(patched_asar))
        print(f"Patched Codex Desktop copied to: {patched_dir}")
        print(f"Launch it with: {patched_exe}")
    else:
        subprocess.run([npx_cmd, "--yes", "asar", "pack", str(workdir), str(app_asar)], check=True)
        if changed:
            _resign_codex_app()
    return 0


def _terminate_pid(pid: int) -> None:
    if IS_WINDOWS:
        import ctypes

        access = 0x100000 | 0x0001
        handle = ctypes.windll.kernel32.OpenProcess(access, False, pid)
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        os.kill(pid, signal.SIGTERM)


def _kill_windows_processes_under(root: Path) -> None:
    root_ps = str(root.resolve()).replace("'", "''")
    script = (
        f"$root = '{root_ps}'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root) } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _remove_tree_retry(root: Path) -> bool:
    if not root.exists():
        return True
    for _ in range(8):
        try:
            shutil.rmtree(root)
            return True
        except PermissionError:
            if IS_WINDOWS:
                _kill_windows_processes_under(root)
                subprocess.run(
                    ["taskkill", "/IM", "Codex.exe", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            time.sleep(0.5)
    return False


def _prepare_patched_app_dir(app_dir: Path) -> Path:
    candidates = [PATCHED_CODEX_APP_DIR] + [
        RUNTIME_DIR / f"codex-desktop-patched-{index}" / "app" for index in range(2, 20)
    ]
    locked: list[Path] = []
    for patched_dir in candidates:
        root = patched_dir.parent
        if root.exists() and not _remove_tree_retry(root):
            locked.append(root)
            continue
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(app_dir, patched_dir, symlinks=False)
        _set_active_patched_app_dir(patched_dir)
        if locked:
            print(
                "Previous patched copy is still in use and was left on disk: "
                + ", ".join(str(path) for path in locked),
            )
        return patched_dir
    raise SystemExit(
        "Could not create a patched Codex copy because existing patched folders are locked. "
        "Close Codex Desktop and retry."
    )


def restore_codex_app_bundle() -> int:
    _quit_codex_app()
    time.sleep(1.0)
    if IS_WINDOWS:
        roots = _patched_install_roots()
        if not roots:
            print("No patched Codex Desktop copy found.")
        else:
            for root in roots:
                if _remove_tree_retry(root):
                    print(f"Removed patched Codex Desktop copy at {root}.")
                else:
                    print(f"Could not remove {root} (files still in use).", file=sys.stderr)
        PATCHED_CODEX_POINTER.unlink(missing_ok=True)
    else:
        app_asar, app_dir = _find_codex_app_bundle()
        if app_asar is None:
            print("Codex Desktop app bundle not found.", file=sys.stderr)
            return 1
        backup = RUNTIME_DIR / "app.asar.before-codex-shim-model-picker-patch"
        if not backup.exists():
            print(f"No app.asar backup found at {backup}.")
        else:
            app_asar.write_bytes(backup.read_bytes())
            print(f"Restored {app_asar} from {backup}.")
    restore_codex_config()
    print("Run: codex-shim stop — then launch Codex from the Start menu.")
    return 0


def _has_command(command: str) -> bool:
    from shutil import which

    return which(command) is not None


def _app_asar_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_model_queries_bundle(workdir: Path, needle: str, replacement: str) -> Path | None:
    assets_dir = workdir / "webview" / "assets"
    if not assets_dir.exists():
        return None
    candidates = sorted(assets_dir.glob("model-queries-*.js"))
    candidates.extend(p for p in sorted(assets_dir.glob("*.js")) if p not in candidates)
    for path in candidates:
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            text = path.read_text(errors="ignore")
        if needle in text or replacement in text:
            return path
    return None


def _find_codex_app_bundle() -> tuple[Path | None, Path | None]:
    """Return (app.asar path, app root directory) for Codex Desktop."""
    if IS_WINDOWS:
        windows_apps = Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "WindowsApps"
        if windows_apps.exists():
            try:
                for entry in sorted(windows_apps.iterdir()):
                    if entry.is_dir() and entry.name.startswith("OpenAI.Codex_"):
                        app_dir = entry / "app"
                        asar = app_dir / "resources" / "app.asar"
                        if asar.exists():
                            return asar, app_dir
            except PermissionError:
                pass
            for entry in sorted(windows_apps.glob("OpenAI.Codex_*/app")):
                asar = entry / "resources" / "app.asar"
                if asar.exists():
                    return asar, entry
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-AppxPackage -Name OpenAI.Codex).InstallLocation",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            install_location = result.stdout.strip()
            if install_location:
                app_dir = Path(install_location) / "app"
                asar = app_dir / "resources" / "app.asar"
                if asar.exists():
                    return asar, app_dir
        local_programs = Path.home() / "AppData" / "Local" / "Programs" / "Codex"
        asar = local_programs / "resources" / "app.asar"
        if asar.exists():
            return asar, local_programs
        return None, None
    app_dir = Path("/Applications/Codex.app")
    asar = app_dir / "Contents" / "Resources" / "app.asar"
    if asar.exists():
        return asar, app_dir
    return None, None


def _resign_codex_app() -> None:
    # Electron validates app.asar through the bundle signature metadata at
    # startup. Re-sign after patching so the modified archive does not trip the
    # asar integrity check.
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", "/Applications/Codex.app"],
        check=True,
    )
    print("Re-signed Codex.app after patch.")


def _patch_asar_integrity_bypass(exe_path: Path, old_hash: str, new_hash: str) -> None:
    if not exe_path.exists():
        print(f"Codex executable not found at {exe_path}; skipping integrity bypass.", file=sys.stderr)
        return
    exe_bytes = exe_path.read_bytes()
    old_hash_bytes = old_hash.encode("ascii")
    new_hash_bytes = new_hash.encode("ascii")
    if old_hash_bytes in exe_bytes:
        exe_bytes = exe_bytes.replace(old_hash_bytes, new_hash_bytes)
        exe_path.write_bytes(exe_bytes)
        print("Bypassed ASAR integrity check in Codex.exe.")
        return
    for match in re.finditer(b"[a-f0-9]{64}", exe_bytes):
        candidate = match.group()
        if _looks_like_test_vector(candidate):
            continue
        exe_bytes = exe_bytes.replace(candidate, new_hash_bytes)
        exe_path.write_bytes(exe_bytes)
        print("Bypassed ASAR integrity check in Codex.exe (auto-detected hash).")
        return
    print("Could not find original asar hash in Codex.exe; integrity bypass may fail.", file=sys.stderr)


def _looks_like_test_vector(candidate: bytes) -> bool:
    if candidate.startswith(b"0123456789abcdef"):
        return True
    try:
        chunks = [int(candidate[i : i + 2], 16) for i in range(0, 64, 2)]
    except ValueError:
        return True
    if len(chunks) != 32:
        return True
    if all(chunks[i + 1] - chunks[i] == 1 for i in range(len(chunks) - 1)):
        return True
    raw = bytes.fromhex(candidate.decode("ascii"))
    return all(b in (0, 1) for b in raw)


def _foreground_codex_app() -> None:
    if IS_WINDOWS:
        return
    script = '''
tell application "Codex" to activate
delay 0.5
tell application "System Events"
  if exists process "Codex" then
    tell process "Codex"
      set frontmost to true
      if (count of windows) is 0 then
        keystroke "n" using command down
        delay 0.3
      end if
      if (count of windows) > 0 then
        set position of window 1 to {80, 60}
        set size of window 1 to {1400, 980}
      end if
    end tell
  end if
end tell
'''
    try:
        subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _managed_config_blocks(default_slug: str, port: int) -> tuple[str, str]:
    catalog_path_str = CATALOG_PATH.as_posix()
    top_block = f'''{MANAGED_BEGIN}
model = "{default_slug}"
model_provider = "factory_byok_shim"
model_catalog_json = "{catalog_path_str}"
{MANAGED_END}
'''

    provider_block = f'''{MANAGED_BEGIN}
[model_providers.factory_byok_shim]
name = "Factory BYOK Shim"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
experimental_bearer_token = "dummy"
request_max_retries = 3
stream_max_retries = 3
stream_idle_timeout_ms = 600000
{MANAGED_END}
'''
    return top_block, provider_block


def _remove_managed_config(text: str) -> str:
    while MANAGED_BEGIN in text:
        before, rest = text.split(MANAGED_BEGIN, 1)
        if MANAGED_END not in rest:
            return before
        _, after = rest.split(MANAGED_END, 1)
        text = before + after
    return text


def _remove_top_level_keys(text: str, keys: set[str]) -> str:
    lines = text.splitlines()
    output: list[str] = []
    in_top_level = True
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_top_level = False
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if in_top_level and key in keys:
            continue
        output.append(line)
    return "\n".join(output) + ("\n" if text.endswith("\n") else "")


def _remove_section(text: str, section: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    skipping = False
    header = f"[{section}]"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = stripped == header
            if skipping:
                continue
        if not skipping:
            output.append(line)
    return "\n".join(output) + ("\n" if text.endswith("\n") else "")


def _override_args(settings_path: Path, port: int) -> list[str]:
    models = FactorySettings(settings_path).load()
    default_slug = default_model_slug(models)
    pairs = codex_config_overrides(CATALOG_PATH, default_slug, port)
    args: list[str] = []
    for pair in pairs:
        args.extend(["-c", pair])
    return args


def _resolve_model_slug(models, requested: str | None) -> str:
    if requested is None:
        return _current_managed_model() or default_model_slug(models)
    by_slug = {model.slug: model.slug for model in models}
    by_model = {}
    for model in models:
        by_model.setdefault(model.model, []).append(model.slug)
    if requested in by_slug:
        return requested
    if requested in by_model and len(by_model[requested]) == 1:
        return by_model[requested][0]
    matches = [model.slug for model in models if requested.lower() in model.display_name.lower()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise SystemExit(f"Ambiguous model {requested!r}. Matches: {', '.join(matches)}")
    raise SystemExit(f"Unknown shim model {requested!r}. Run: codex-shim model list")


def _current_managed_model() -> str | None:
    if not CODEX_CONFIG_PATH.exists():
        return None
    for line in CODEX_CONFIG_PATH.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("model = "):
            return stripped.split("=", 1)[1].strip().strip('"')
    return None


def _server_runtime_ready() -> bool:
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        print(f"aiohttp is not installed for {sys.executable}.", file=sys.stderr)
        print(f"  {sys.executable} -m pip install aiohttp", file=sys.stderr)
        return False
    return True


def _print_log_tail(lines: int = 30) -> None:
    if not LOG_PATH.exists():
        return
    try:
        tail = LOG_PATH.read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return
    if not tail:
        return
    print(f"--- last {len(tail)} line(s) from {LOG_PATH} ---", file=sys.stderr)
    for line in tail:
        print(line, file=sys.stderr)


def _healthy(port: int) -> bool:
    try:
        with urlopen(f"http://{DEFAULT_HOST}:{port}/health", timeout=0.5) as response:
            return response.status == 200
    except Exception:
        return False


def _read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text().strip())
    except Exception:
        return None


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if IS_WINDOWS:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
