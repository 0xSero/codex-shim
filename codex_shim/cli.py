from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import hashlib
from urllib.request import urlopen

from .catalog import codex_config_overrides, write_catalog, write_config
from .settings import (
    CHATGPT_MODEL_SLUG,
    DEFAULT_FACTORY_SETTINGS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    FactorySettings,
    chatgpt_passthrough_enabled,
    default_model_slug,
)


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
PREVIOUS_TOP_LEVEL_PREFIX = "# codex-shim previous-top-level = "
MANAGED_TOP_LEVEL_KEYS = {"model", "model_provider", "model_catalog_json"}


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    title: str
    settings_path: Path
    port: int
    placeholder_key: str
    default_model: str
    default_display_name: str
    default_provider: str
    default_base_url: str
    default_context: int
    allowed_providers: frozenset[str]
    template_path: Path


PROVIDER_SPECS = {
    "openrouter": ProviderSpec(
        name="openrouter",
        title="OpenRouter",
        settings_path=RUNTIME_DIR / "openrouter-settings.json",
        port=8766,
        placeholder_key="REPLACE_WITH_OPENROUTER_API_KEY",
        default_model="openai/gpt-4o-mini",
        default_display_name="OpenRouter GPT-4o Mini",
        default_provider="generic-chat-completion-api",
        default_base_url="https://openrouter.ai/api/v1",
        default_context=128000,
        allowed_providers=frozenset({"generic-chat-completion-api", "openai"}),
        template_path=PROJECT_ROOT / "examples" / "openrouter-settings.example.json",
    ),
    "minimax": ProviderSpec(
        name="minimax",
        title="MiniMax Token Plan",
        settings_path=RUNTIME_DIR / "minimax-settings.json",
        port=8767,
        placeholder_key="REPLACE_WITH_MINIMAX_TOKEN_PLAN_KEY",
        default_model="MiniMax-M2.7",
        default_display_name="MiniMax M2.7",
        default_provider="minimax",
        default_base_url="https://api.minimax.io/v1",
        default_context=1000000,
        allowed_providers=frozenset({"minimax", "generic-chat-completion-api", "openai"}),
        template_path=PROJECT_ROOT / "examples" / "minimax-token-plan-settings.example.json",
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-shim")
    parser.add_argument("--settings", type=Path, default=DEFAULT_FACTORY_SETTINGS)
    parser.add_argument("--port", type=int)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate")
    sub.add_parser("list")
    sub.add_parser("start")
    sub.add_parser("enable", help="Install managed shim blocks into ~/.codex/config.toml and start the shim.")
    sub.add_parser("stop")
    sub.add_parser("disable")
    sub.add_parser("restart")
    sub.add_parser("status")
    sub.add_parser("patch-app", help="Experimental/incomplete: patch Codex Desktop ASAR model picker.")
    sub.add_parser("restore-app", help="Restore Codex Desktop app.asar from the pre-patch backup.")

    model_parser = sub.add_parser("model", help="List or set the active shim model in Codex config.")
    model_sub = model_parser.add_subparsers(dest="model_command", required=True)
    model_sub.add_parser("list")
    use_parser = model_sub.add_parser("use")
    use_parser.add_argument("model_slug")

    codex_parser = sub.add_parser("codex", help="Run Codex CLI with opt-in shim config overrides.")
    codex_parser.add_argument("args", nargs=argparse.REMAINDER)

    app_parser = sub.add_parser("app", help="Install managed shim config blocks, then launch Codex Desktop.")
    app_parser.add_argument("-m", "--model", dest="model_slug")
    app_parser.add_argument("path", nargs="?", default=".")

    setup_parser = sub.add_parser("setup", help="Configure an ignored local provider settings file.")
    setup_parser.add_argument("provider", choices=sorted(PROVIDER_SPECS))

    run_parser = sub.add_parser("run", help="Run Codex CLI through a configured provider.")
    run_parser.add_argument("provider", choices=sorted(PROVIDER_SPECS))
    run_parser.add_argument("args", nargs=argparse.REMAINDER)

    for provider_name, spec in PROVIDER_SPECS.items():
        provider_alias = sub.add_parser(provider_name, help=f"Run Codex CLI through {spec.title}.")
        provider_alias.add_argument("args", nargs=argparse.REMAINDER)

    provider_parser = sub.add_parser("provider", help="List built-in provider workflows.")
    provider_sub = provider_parser.add_subparsers(dest="provider_command", required=True)
    provider_sub.add_parser("list")

    args = parser.parse_args(argv)
    port = args.port if args.port is not None else DEFAULT_PORT
    if args.command == "generate":
        generate(args.settings, port)
        return 0
    if args.command == "list":
        return list_models(args.settings)
    if args.command in {"start", "enable"}:
        generate(args.settings, port)
        code = start(args.settings, port)
        if code == 0 and args.command == "enable":
            install_codex_config(args.settings, port)
        return code
    if args.command in {"stop", "disable"}:
        if args.command == "disable":
            restore_codex_config()
        return stop()
    if args.command == "restart":
        stop()
        generate(args.settings, port)
        return start(args.settings, port)
    if args.command == "status":
        return status(port)
    if args.command == "patch-app":
        return patch_codex_app()
    if args.command == "restore-app":
        return restore_codex_app_bundle()
    if args.command == "model":
        if args.model_command == "list":
            return list_models(args.settings)
        if args.model_command == "use":
            generate(args.settings, port)
            ensure_started(args.settings, port)
            install_codex_config(args.settings, port, args.model_slug)
            print(f"Active Codex shim model: {args.model_slug}")
            return 0
    if args.command == "codex":
        generate(args.settings, port)
        ensure_started(args.settings, port)
        exec_codex(args.settings, port, args.args)
        return 0
    if args.command == "app":
        generate(args.settings, port)
        ensure_started(args.settings, port)
        install_codex_config(args.settings, port, args.model_slug)
        exec_codex_app(args.settings, port, args.path)
        return 0
    if args.command == "setup":
        return setup_provider(args.provider)
    if args.command == "run":
        return run_provider(args.provider, args.args, args.port)
    if args.command in PROVIDER_SPECS:
        return run_provider(args.command, args.args, args.port)
    if args.command == "provider":
        if args.provider_command == "list":
            return list_providers()
        return 0
    return 2


def list_providers() -> int:
    width = max(len(name) for name in PROVIDER_SPECS)
    for spec in PROVIDER_SPECS.values():
        print(f"{spec.name:<{width}}  {spec.title}  ->  {spec.settings_path}")
    return 0


def setup_provider(provider: str) -> int:
    spec = PROVIDER_SPECS[provider]
    status = _provider_settings_status(spec)
    _prompt_provider_setup(spec, status)
    status = _provider_settings_status(spec)
    if status != "ok":
        print(f"{spec.title} settings are still not usable: {status}", file=sys.stderr)
        return 1
    print(f"{spec.title} settings configured. Run: codex-shim run {spec.name} .")
    return 0


def run_provider(provider: str, codex_args: list[str], requested_port: int | None = None) -> int:
    spec = PROVIDER_SPECS[provider]
    status = _provider_settings_status(spec)
    if status != "ok":
        _prompt_provider_setup(spec, status)
        status = _provider_settings_status(spec)
    if status != "ok":
        print(f"{spec.title} settings are still not usable: {status}", file=sys.stderr)
        return 1

    os.environ.setdefault("CODEX_SHIM_DISABLE_CHATGPT", "1")
    port = requested_port if requested_port is not None else spec.port
    generate(spec.settings_path, port)
    ensure_started(spec.settings_path, port)
    model = os.environ.get("CODEX_SHIM_MODEL") or _first_model_slug(spec.settings_path)
    if not model:
        print(f"No model slug found in {spec.settings_path}.", file=sys.stderr)
        return 1
    codex_args = list(codex_args or [])
    if codex_args[:1] == ["--"]:
        codex_args = codex_args[1:]
    exec_codex(spec.settings_path, port, ["-m", model, *codex_args])
    return 0


def _provider_settings_status(spec: ProviderSpec) -> str:
    if not spec.settings_path.exists():
        return "missing"
    try:
        data = json.loads(spec.settings_path.read_text())
    except Exception:
        return "invalid_json"
    models = data.get("customModels")
    if not isinstance(models, list) or not models:
        return "empty"
    row = models[0] if isinstance(models[0], dict) else {}
    if not str(row.get("model") or "").strip():
        return "missing_model"
    provider = str(row.get("provider") or "").strip()
    if not provider:
        return "missing_provider"
    if provider not in spec.allowed_providers:
        return "unsupported_provider"
    if not str(row.get("baseUrl") or "").strip():
        return "missing_base_url"
    api_key = str(row.get("apiKey") or "").strip()
    if not api_key or api_key == spec.placeholder_key:
        return "missing_key"
    return "ok"


def _prompt_provider_setup(spec: ProviderSpec, status: str) -> None:
    if not sys.stdin.isatty():
        print(
            f"{spec.title} settings are not configured ({status}):\n"
            f"  {spec.settings_path}\n\n"
            "Run this helper from an interactive terminal, or create the file manually from:\n"
            f"  {spec.template_path}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if status == "ok":
        print(
            f"{spec.title} setup:\n"
            f"  {spec.settings_path}\n\n"
            "This will update the local ignored settings file. The API key will not be echoed.",
            file=sys.stderr,
        )
    else:
        print(
            f"{spec.title} settings need configuration ({status}):\n"
            f"  {spec.settings_path}\n\n"
            "This will write a local ignored settings file. The API key will not be echoed.",
            file=sys.stderr,
        )

    current = _current_provider_settings(spec)
    key_prompt = f"{spec.title} API key"
    current_key = current.get("apiKey", "")
    if current_key and current_key != spec.placeholder_key:
        key_prompt += " [keep existing]"
    api_key = getpass.getpass(f"{key_prompt}: ")
    if not api_key and current_key and current_key != spec.placeholder_key:
        api_key = current_key
    if not api_key:
        print("API key is required.", file=sys.stderr)
        raise SystemExit(1)

    model = _prompt_with_default(f"{spec.title} model", current["model"])
    display_name = _prompt_with_default("Display name", current["displayName"])
    base_url = _prompt_with_default("Base URL", current["baseUrl"])
    context_raw = _prompt_with_default("Max context tokens", str(current["maxContextLimit"]))
    try:
        context = int(context_raw)
    except ValueError:
        context = spec.default_context

    _write_provider_settings(spec, api_key, model, display_name, base_url, context)
    print(f"Wrote {spec.settings_path}", file=sys.stderr)


def _current_provider_settings(spec: ProviderSpec) -> dict[str, str]:
    row = {}
    if spec.settings_path.exists():
        try:
            data = json.loads(spec.settings_path.read_text())
            models = data.get("customModels") or []
            if models and isinstance(models[0], dict):
                row = models[0]
        except Exception:
            row = {}
    return {
        "apiKey": str(row.get("apiKey") or ""),
        "model": str(row.get("model") or spec.default_model),
        "displayName": str(row.get("displayName") or spec.default_display_name),
        "baseUrl": str(row.get("baseUrl") or spec.default_base_url),
        "maxContextLimit": str(row.get("maxContextLimit") or spec.default_context),
    }


def _prompt_with_default(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ")
    return value or default


def _write_provider_settings(
    spec: ProviderSpec,
    api_key: str,
    model: str,
    display_name: str,
    base_url: str,
    context: int,
) -> None:
    spec.settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        spec.settings_path.parent.chmod(0o700)
    except OSError:
        pass
    old_umask = os.umask(0o077)
    try:
        payload = {
            "customModels": [
                {
                    "model": model,
                    "provider": spec.default_provider,
                    "baseUrl": base_url.rstrip("/"),
                    "apiKey": api_key,
                    "displayName": display_name,
                    "maxContextLimit": context,
                }
            ]
        }
        spec.settings_path.write_text(json.dumps(payload, indent=2) + "\n")
    finally:
        os.umask(old_umask)


def _first_model_slug(settings_path: Path) -> str | None:
    models = FactorySettings(settings_path).load()
    return models[0].slug if models else None


def generate(settings_path: Path, port: int) -> None:
    models = FactorySettings(settings_path).load()
    include_chatgpt = chatgpt_passthrough_enabled()
    try:
        default_model_slug(models, include_chatgpt)
        write_catalog(models, CATALOG_PATH, include_chatgpt)
        write_config(models, CONFIG_PATH, CATALOG_PATH, port, include_chatgpt)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
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
    cleaned = _remove_managed_config(original)
    current_top_level = _extract_top_level_key_lines(cleaned, MANAGED_TOP_LEVEL_KEYS)
    if current_top_level:
        previous_top_level = current_top_level
    elif MANAGED_BEGIN in original:
        previous_top_level = _managed_previous_top_level(original)
    else:
        previous_top_level = _extract_top_level_key_lines(original, MANAGED_TOP_LEVEL_KEYS)
    cleaned = _remove_top_level_keys(cleaned, MANAGED_TOP_LEVEL_KEYS)
    cleaned = _remove_section(cleaned, "model_providers.factory_byok_shim")
    top_block, provider_block = _managed_config_blocks(default_slug, port, previous_top_level)
    CODEX_CONFIG_PATH.write_text(top_block + "\n" + cleaned.lstrip() + "\n" + provider_block)
    print(f"Installed shim config into {CODEX_CONFIG_PATH}.")


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
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_PATH.open("ab")
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
    process = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env, stdout=log, stderr=log, start_new_session=True)
    PID_PATH.write_text(str(process.pid))
    for _ in range(50):
        if _healthy(port):
            print(f"Shim started on http://{DEFAULT_HOST}:{port} with pid {process.pid}.")
            print(f"Log: {LOG_PATH}")
            return 0
        if process.poll() is not None:
            print(f"Shim exited during startup. See {LOG_PATH}.", file=sys.stderr)
            return 1
        time.sleep(0.1)
    print(f"Shim process started but health check timed out. See {LOG_PATH}.", file=sys.stderr)
    return 1


def stop() -> int:
    pid = _read_pid()
    if not _pid_running(pid):
        print("Shim is not running.")
        PID_PATH.unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _pid_running(pid):
            PID_PATH.unlink(missing_ok=True)
            print("Shim stopped.")
            return 0
        time.sleep(0.1)
    print(f"Shim pid {pid} did not exit after SIGTERM.", file=sys.stderr)
    return 1


def restore_codex_config() -> None:
    if CODEX_CONFIG_PATH.exists():
        current = CODEX_CONFIG_PATH.read_text()
        previous_top_level = _managed_previous_top_level(current)
        restored = _remove_managed_config(current)
        restored = _remove_section(restored, "model_providers.factory_byok_shim")
        restored = _restore_missing_top_level_keys(restored.lstrip(), previous_top_level)
        CODEX_CONFIG_PATH.write_text(restored)
        print(f"Removed shim config from {CODEX_CONFIG_PATH}.")
    if CODEX_CONFIG_BACKUP_PATH.exists():
        CODEX_CONFIG_BACKUP_PATH.unlink()
        print(f"Removed stale shim backup {CODEX_CONFIG_BACKUP_PATH}.")


def status(port: int) -> int:
    pid = _read_pid()
    if _pid_running(pid) and _healthy(port):
        print(f"Shim is running on http://{DEFAULT_HOST}:{port} with pid {pid}.")
        return 0
    if _pid_running(pid):
        print(f"Shim process {pid} exists but health check failed.")
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
    args = ["codex", "app", path]
    subprocess.Popen(args)
    _foreground_codex_app()


def _quit_codex_app() -> None:
    script = 'tell application "Codex" to if it is running then quit'
    try:
        subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
    except OSError:
        pass


def patch_codex_app() -> int:
    app_asar = Path("/Applications/Codex.app/Contents/Resources/app.asar")
    backup = RUNTIME_DIR / "app.asar.before-codex-shim-model-picker-patch"
    workdir = RUNTIME_DIR / "app-asar-work"
    needle = "let u=c.useHiddenModels&&o!==`amazonBedrock`,d;"
    replacement = "let u=!1,d;"

    if not app_asar.exists():
        print(f"Codex app bundle not found at {app_asar}.", file=sys.stderr)
        return 1
    if not _has_command("npx"):
        print("npx is required to patch the Electron asar bundle.", file=sys.stderr)
        return 1

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
        import shutil

        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    subprocess.run(["npx", "--yes", "asar", "extract", str(app_asar), str(workdir)], check=True)
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
        subprocess.run(["npx", "--yes", "asar", "pack", str(workdir), str(app_asar)], check=True)
        changed = True
        print("Patched Codex Desktop model picker allowlist filter.")
    else:
        print("Could not find the expected model picker filter in Codex Desktop.", file=sys.stderr)
        return 1
    if changed:
        _resign_codex_app()
    return 0


def restore_codex_app_bundle() -> int:
    app_asar = Path("/Applications/Codex.app/Contents/Resources/app.asar")
    backup = RUNTIME_DIR / "app.asar.before-codex-shim-model-picker-patch"
    if not backup.exists():
        print(f"No app.asar backup found at {backup}.")
        return 0
    _quit_codex_app()
    app_asar.write_bytes(backup.read_bytes())
    print(f"Restored {app_asar} from {backup}.")
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


def _resign_codex_app() -> None:
    # Electron validates app.asar through the bundle signature metadata at
    # startup. Re-sign after patching so the modified archive does not trip the
    # asar integrity check.
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", "/Applications/Codex.app"],
        check=True,
    )
    print("Re-signed Codex.app after patch.")


def _foreground_codex_app() -> None:
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


def _managed_config_blocks(default_slug: str, port: int, previous_top_level: dict[str, str] | None = None) -> tuple[str, str]:
    metadata = ""
    if previous_top_level:
        metadata = PREVIOUS_TOP_LEVEL_PREFIX + json.dumps(previous_top_level, sort_keys=True) + "\n"
    top_block = f'''{MANAGED_BEGIN}
{metadata}model = "{default_slug}"
model_provider = "factory_byok_shim"
model_catalog_json = "{CATALOG_PATH}"
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


def _extract_top_level_key_lines(text: str, keys: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    in_top_level = True
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_top_level = False
        if not in_top_level or not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in keys:
            found[key] = line
    return found


def _managed_previous_top_level(text: str) -> dict[str, str]:
    in_managed = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == MANAGED_BEGIN:
            in_managed = True
            continue
        if stripped == MANAGED_END:
            in_managed = False
            continue
        if in_managed and stripped.startswith(PREVIOUS_TOP_LEVEL_PREFIX):
            encoded = stripped[len(PREVIOUS_TOP_LEVEL_PREFIX) :]
            try:
                payload = json.loads(encoded)
            except json.JSONDecodeError:
                return {}
            if isinstance(payload, dict):
                return {str(k): str(v) for k, v in payload.items() if k in MANAGED_TOP_LEVEL_KEYS}
    return {}


def _restore_missing_top_level_keys(text: str, previous_top_level: dict[str, str]) -> str:
    if not previous_top_level:
        return text
    current = _extract_top_level_key_lines(text, MANAGED_TOP_LEVEL_KEYS)
    lines = [
        previous_top_level[key]
        for key in ("model", "model_provider", "model_catalog_json")
        if key in previous_top_level and key not in current
    ]
    if not lines:
        return text
    prefix = "\n".join(lines) + "\n"
    if text and not text.startswith("\n"):
        return prefix + text
    return prefix + text.lstrip()


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
        current = _current_managed_model()
        valid = _valid_model_slugs(models)
        if current in valid:
            return current
        return default_model_slug(models)
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
    in_managed = False
    for line in CODEX_CONFIG_PATH.read_text().splitlines():
        stripped = line.strip()
        if stripped == MANAGED_BEGIN:
            in_managed = True
            continue
        if stripped == MANAGED_END:
            in_managed = False
            continue
        if in_managed and stripped.startswith("model = "):
            return stripped.split("=", 1)[1].strip().strip('"')
    return None


def _valid_model_slugs(models) -> set[str]:
    slugs = {model.slug for model in models}
    if chatgpt_passthrough_enabled():
        slugs.add(CHATGPT_MODEL_SLUG)
    return slugs


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
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
