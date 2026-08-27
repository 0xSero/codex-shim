from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import ctypes
import signal
import shutil
import subprocess
import sys
import time
import hashlib
import json
import plistlib
import re
import struct
from urllib.request import urlopen

from . import router as router_module
from .catalog import _toml_escape, codex_config_overrides, write_catalog, write_config
from .cursor_passthrough import (
    cursor_passthrough_available,
    cursor_passthrough_display_names,
    is_cursor_passthrough_slug,
)
from .settings import (
    CHATGPT_MODEL_SLUG,
    DEFAULT_SETTINGS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_CODEX_AUTH,
    PROVIDER_NAME,
    ModelSettings,
    available_model_slugs,
    chatgpt_passthrough_available,
    chatgpt_passthrough_display_names,
    chatgpt_passthrough_slugs,
    default_model_slug,
    is_chatgpt_passthrough_slug,
    usable_byok_models,
    byok_model_has_credentials,
)
from .opencode_go import (
    OPENCODE_GO_API_KEY_ENV,
    OPENCODE_GO_BASE_URL,
    refresh_opencode_go_settings,
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
WINDOWS_PROCESS_TERMINATE = 0x0001
WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WINDOWS_STILL_ACTIVE = 259
PREVIOUS_TOP_LEVEL_PREFIX = "# codex-shim previous-top-level = "
MANAGED_TOP_LEVEL_KEYS = {"model", "model_provider", "model_catalog_json"}
APP_ASAR_BACKUP_NAME = "app.asar.before-codex-shim-model-picker-patch"
INFO_PLIST_BACKUP_NAME = "Info.plist.before-codex-shim-model-picker-patch"
SYSTEM_CODEX_APP = Path("/Applications/Codex.app")
USER_CODEX_APP = Path.home() / "Applications" / "Codex.app"
# Pin the ASAR packer used to rewrite the Codex Desktop app bundle. An unpinned
# `npx --yes asar` resolves whatever the registry currently serves and runs it
# immediately with the user's privileges against a trusted application bundle —
# a supply-chain hazard. Pin to an exact, auditable version (overridable via
# CODEX_SHIM_ASAR_PACKAGE for users who must use a mirror or a newer release).
DEFAULT_ASAR_PACKAGE = "@electron/asar@4.2.0"
ASAR_PACKAGE_ENV = "CODEX_SHIM_ASAR_PACKAGE"
MODEL_PICKER_NEEDLE = re.compile(
    r"(?P<lhs>(?:let )?\w+=)"
    r"(?:\w+\.useHiddenModels|\w+)"
    r"&&\w+!==`amazonBedrock`"
    r"(?P<sep>[,;])"
)
MODEL_PICKER_REPLACEMENT = r"\g<lhs>!1\g<sep>"
MODEL_PICKER_APPLIED = re.compile(
    r"(?:let )?\w+=!1[,;][^\n]{0,300}\.forEach"
)

SIDEBAR_RECENT_THREADS_NEEDLE = re.compile(
    r"listRecentThreads\(\{cursor:e,limit:t(?:,useStateDbOnly:\w+(?:=!\d)?)?\}\)\{return this\.params\.requestClient\.sendRequest\(`thread/list`,"
    r"\{limit:t,cursor:e,sortKey:this\.recentConversationSortKey,modelProviders:null,archived:!1,sourceKinds:(\w+)(?:,useStateDbOnly:\w+)?\}\)\}"
)
SIDEBAR_RECENT_THREADS_REPLACEMENT = (
    r"listRecentThreads({cursor:e,limit:t}){return this.params.requestClient.sendRequest(`thread/list`,"
    r"{limit:t,cursor:e,sortKey:this.recentConversationSortKey,modelProviders:[],archived:!1,sourceKinds:\1})}"
)
SIDEBAR_RECENT_THREADS_APPLIED = re.compile(
    r"\.recentConversationSortKey,modelProviders:\[\],archived:!1,sourceKinds:\w+"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-shim")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
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
    sub.add_parser("doctor", help="Print a read-only local diagnostics report.")
    sub.add_parser("patch-app", help="Patch Codex Desktop picker/sidebar handling for custom shim models.")
    sub.add_parser("restore-app", help="Restore Codex Desktop app.asar from the pre-patch backup.")

    opencode_parser = sub.add_parser("opencode-go", help="Discover and configure OpenCode Go models.")
    opencode_sub = opencode_parser.add_subparsers(dest="opencode_go_command", required=True)
    refresh_parser = opencode_sub.add_parser("refresh", help="Refresh OpenCode Go models into the settings file.")
    refresh_parser.add_argument("--api-key-env", default=OPENCODE_GO_API_KEY_ENV)
    refresh_parser.add_argument("--base-url", default=OPENCODE_GO_BASE_URL)
    refresh_parser.add_argument("--prefer", choices=["chat", "messages"], default="chat")
    refresh_parser.add_argument("--timeout", type=float, default=30.0)

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
    if args.command == "doctor":
        return doctor(args.settings, args.port)
    if args.command == "patch-app":
        return patch_codex_app()
    if args.command == "restore-app":
        return restore_codex_app_bundle()
    if args.command == "opencode-go":
        if args.opencode_go_command == "refresh":
            return refresh_opencode_go(args.settings, args.api_key_env, args.base_url, args.prefer, args.timeout)
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


def _load_models(settings_path: Path):
    expanded = Path(settings_path).expanduser()
    try:
        return ModelSettings(expanded).load()
    except FileNotFoundError as exc:
        raise SystemExit(
            f"Settings file not found: {expanded}\n"
            "Create ~/.codex-shim/models.json, or pass --settings /path/to/models.json."
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Settings file is not valid JSON: {expanded}: {exc}") from exc


def _active_router(models, settings_path: Path):
    """RouterConfig when the Auto Router is enabled and has a usable candidate."""
    config = router_module.load_router_config(Path(settings_path).expanduser())
    if config and router_module.router_is_active(config, available_model_slugs(models)):
        return config
    return None


@dataclass(frozen=True)
class DoctorCheck:
    section: str
    status: str  # OK | WARN | FAIL | INFO
    message: str
    detail: str = ""


def doctor(settings_path: Path, port: int) -> int:
    """Print a read-only diagnostics report for the local codex-shim setup."""
    expanded = Path(settings_path).expanduser()
    checks: list[DoctorCheck] = []
    checks.extend(_doctor_python())
    checks.extend(_doctor_dependencies())
    checks.extend(_doctor_codex_cli())
    checks.extend(_doctor_settings(expanded))
    checks.extend(_doctor_runtime_files())
    checks.extend(_doctor_daemon(port))
    checks.extend(_doctor_chatgpt())
    checks.extend(_doctor_cursor())
    checks.extend(_doctor_proxy_env())
    checks.extend(_doctor_codex_config())
    _print_doctor_report(checks)
    return 1 if any(check.status == "FAIL" for check in checks) else 0


def _doctor_python() -> list[DoctorCheck]:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    status = "OK" if sys.version_info >= (3, 11) else "FAIL"
    detail = "" if status == "OK" else "codex-shim requires Python 3.11+"
    return [
        DoctorCheck("Python", status, f"version: {version}", detail),
        DoctorCheck("Python", "OK", f"executable: {sys.executable}"),
    ]


def _doctor_dependencies() -> list[DoctorCheck]:
    if importlib.util.find_spec("aiohttp") is None:
        return [
            DoctorCheck(
                "Dependencies",
                "FAIL",
                "aiohttp is not importable",
                "Try: python3 -m pip install -e .",
            )
        ]
    return [DoctorCheck("Dependencies", "OK", "aiohttp importable")]


def _doctor_codex_cli() -> list[DoctorCheck]:
    found = shutil.which("codex")
    if not found:
        return [
            DoctorCheck(
                "Codex CLI",
                "WARN",
                "codex not found on PATH",
                "Install and authenticate Codex before using codex-shim app/codex flows.",
            )
        ]
    checks = [DoctorCheck("Codex CLI", "OK", f"found: {found}")]
    try:
        result = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        checks.append(DoctorCheck("Codex CLI", "WARN", "could not run codex --version", str(exc)))
        return checks
    output = (result.stdout or result.stderr).strip().splitlines()
    version = output[0].strip() if output else "unknown"
    if len(version) > 200:
        version = version[:197] + "..."
    if result.returncode == 0:
        checks.append(DoctorCheck("Codex CLI", "OK", f"version: {version}"))
    else:
        checks.append(DoctorCheck("Codex CLI", "WARN", "codex --version failed", version))
    return checks


def _doctor_settings(settings_path: Path) -> list[DoctorCheck]:
    section = "Settings"
    path = settings_path.expanduser()
    if not path.exists():
        detail = "Create ~/.codex-shim/models.json or run codex login for ChatGPT passthrough-only use."
        return [DoctorCheck(section, "WARN", f"settings file not found: {path}", detail)]
    checks = [DoctorCheck(section, "OK", f"path: {path}")]
    try:
        models = _load_models(path)
    except SystemExit as exc:
        message = str(exc)
        if "not valid JSON" in message or "invalid JSON" in message:
            return [DoctorCheck(section, "FAIL", f"invalid JSON: {path}", message)]
        return [DoctorCheck(section, "FAIL", f"could not load settings: {path}", message)]
    except Exception as exc:
        return [DoctorCheck(section, "FAIL", f"could not load settings: {path}", str(exc))]

    usable = usable_byok_models(models)
    missing_count = len(models) - len(usable)
    checks.append(DoctorCheck(section, "OK", f"configured models: {len(models)}"))
    checks.append(DoctorCheck(section, "OK", f"usable BYOK models: {len(usable)}"))
    if missing_count:
        checks.append(DoctorCheck(section, "WARN", f"models missing API keys: {missing_count}"))
    else:
        checks.append(DoctorCheck(section, "OK", "models missing API keys: 0"))
    providers = Counter(model.provider for model in models)
    provider_text = ", ".join(f"{provider}={count}" for provider, count in sorted(providers.items())) or "none"
    checks.append(DoctorCheck(section, "INFO", f"providers: {provider_text}"))

    router_config = router_module.load_router_config(path)
    if router_config is None:
        checks.append(DoctorCheck(section, "INFO", "auto router configured: false"))
    else:
        active = _active_router(models, path)
        if active is not None:
            checks.append(DoctorCheck(section, "OK", f"auto router active: {active.slug}"))
        elif router_config.effective_enabled:
            checks.append(
                DoctorCheck(
                    section,
                    "WARN",
                    f"auto router configured but inactive: {router_config.slug}",
                    "Ensure at least one router candidate matches a usable model slug.",
                )
            )
        else:
            checks.append(DoctorCheck(section, "INFO", f"auto router configured but disabled: {router_config.slug}"))
    return checks


def _doctor_runtime_files() -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    if CATALOG_PATH.exists():
        checks.append(DoctorCheck("Runtime files", "OK", f"catalog: {CATALOG_PATH}"))
        try:
            data = json.loads(CATALOG_PATH.read_text())
            models = data.get("models", []) if isinstance(data, dict) else []
            count = len(models) if isinstance(models, list) else 0
            checks.append(DoctorCheck("Runtime files", "OK", f"catalog models: {count}"))
        except (OSError, json.JSONDecodeError) as exc:
            checks.append(
                DoctorCheck("Runtime files", "WARN", f"catalog JSON is not readable: {CATALOG_PATH}", str(exc))
            )
    else:
        checks.append(DoctorCheck("Runtime files", "INFO", f"catalog missing: {CATALOG_PATH}"))
    if CONFIG_PATH.exists():
        checks.append(DoctorCheck("Runtime files", "OK", f"config: {CONFIG_PATH}"))
    else:
        checks.append(DoctorCheck("Runtime files", "INFO", f"config missing: {CONFIG_PATH}"))
    if PID_PATH.exists():
        checks.append(DoctorCheck("Runtime files", "INFO", f"pid file: {PID_PATH}"))
    else:
        checks.append(DoctorCheck("Runtime files", "INFO", f"pid file missing: {PID_PATH}"))
    if LOG_PATH.exists():
        checks.append(DoctorCheck("Runtime files", "INFO", f"log file: {LOG_PATH}"))
    else:
        checks.append(DoctorCheck("Runtime files", "INFO", f"log file missing: {LOG_PATH}"))
    return checks


def _doctor_daemon(port: int) -> list[DoctorCheck]:
    checks = [DoctorCheck("Shim daemon", "INFO", f"health URL: http://{DEFAULT_HOST}:{port}/health")]
    pid = _read_pid()
    if pid is None:
        checks.append(DoctorCheck("Shim daemon", "INFO", f"pid file missing or unreadable: {PID_PATH}"))
    elif _pid_running(pid):
        checks.append(DoctorCheck("Shim daemon", "OK", f"pid {pid} is running"))
    else:
        checks.append(DoctorCheck("Shim daemon", "WARN", f"pid {pid} is not running"))

    health = _health(port)
    if health is None:
        checks.append(DoctorCheck("Shim daemon", "WARN", "health endpoint unavailable"))
        return checks
    model_count = _health_model_count(health.get("models"))
    if health.get("ok") is True:
        checks.append(DoctorCheck("Shim daemon", "OK", f"health ok: {model_count} models"))
    else:
        checks.append(DoctorCheck("Shim daemon", "WARN", f"health not ok: {model_count} models"))
    for key in ("chatgpt_passthrough", "cursor_passthrough", "auto_router"):
        if key in health:
            checks.append(DoctorCheck("Shim daemon", "INFO", f"{key}: {_bool_text(health.get(key))}"))
    return checks


def _health_model_count(value) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    return 0


def _doctor_chatgpt() -> list[DoctorCheck]:
    if _env_flag("CODEX_SHIM_DISABLE_CHATGPT"):
        return [DoctorCheck("ChatGPT passthrough", "INFO", "disabled via CODEX_SHIM_DISABLE_CHATGPT")]
    auth_path = Path(DEFAULT_CODEX_AUTH).expanduser()
    if chatgpt_passthrough_available():
        return [DoctorCheck("ChatGPT passthrough", "OK", f"available via {auth_path}")]
    if auth_path.exists():
        detail = "Run `codex login` again if you want ChatGPT/Codex passthrough."
    else:
        detail = "Run `codex login` if you want ChatGPT/Codex passthrough."
    return [DoctorCheck("ChatGPT passthrough", "WARN", "unavailable", detail)]


def _doctor_cursor() -> list[DoctorCheck]:
    if _env_flag("CODEX_SHIM_DISABLE_CURSOR"):
        return [DoctorCheck("Cursor passthrough", "INFO", "disabled via CODEX_SHIM_DISABLE_CURSOR")]
    bin_override = os.environ.get("CURSOR_AGENT_BIN", "").strip()
    agent_bin = bin_override or shutil.which("cursor-agent")
    checks: list[DoctorCheck] = []
    if agent_bin:
        checks.append(DoctorCheck("Cursor passthrough", "INFO", f"cursor-agent: {agent_bin}"))
    else:
        checks.append(DoctorCheck("Cursor passthrough", "WARN", "cursor-agent not found on PATH"))
    if cursor_passthrough_available():
        checks.append(DoctorCheck("Cursor passthrough", "OK", "cursor-agent logged in"))
        for slug in sorted(cursor_passthrough_display_names()):
            checks.append(DoctorCheck("Cursor passthrough", "INFO", f"exposed model: {slug}"))
    else:
        checks.append(
            DoctorCheck(
                "Cursor passthrough",
                "WARN",
                "unavailable",
                "Run `cursor-agent login` if you want Cursor passthrough.",
            )
        )
    return checks


def _doctor_proxy_env() -> list[DoctorCheck]:
    required = {"127.0.0.1", "localhost", "::1"}
    values: set[str] = set()
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key, "")
        for part in raw.split(","):
            value = part.strip().lower()
            if value:
                values.add(value)
    if "*" in values or required <= values:
        return [DoctorCheck("Proxy", "OK", "loopback hosts covered by NO_PROXY/no_proxy")]
    return [
        DoctorCheck(
            "Proxy",
            "WARN",
            "NO_PROXY/no_proxy does not include all loopback hosts",
            "Recommended: 127.0.0.1,localhost,::1",
        )
    ]


def _doctor_codex_config() -> list[DoctorCheck]:
    path = Path(CODEX_CONFIG_PATH).expanduser()
    if not path.exists():
        return [
            DoctorCheck(
                "Codex config",
                "INFO",
                "shim provider is not currently installed",
                "Run `codex-shim app .` or `codex-shim enable` to wire Codex to the shim.",
            )
        ]
    checks = [DoctorCheck("Codex config", "OK", f"config exists: {path}")]
    try:
        text = path.read_text()
    except OSError as exc:
        return [DoctorCheck("Codex config", "WARN", f"could not read config: {path}", str(exc))]
    provider_configured = (
        f'model_provider = "{PROVIDER_NAME}"' in text or f"[model_providers.{PROVIDER_NAME}]" in text
    )
    if provider_configured:
        checks.append(DoctorCheck("Codex config", "OK", "shim provider configured"))
    else:
        checks.append(
            DoctorCheck(
                "Codex config",
                "INFO",
                "shim provider is not currently installed",
                "Run `codex-shim app .` or `codex-shim enable` to wire Codex to the shim.",
            )
        )
    current = _current_managed_model()
    if current:
        checks.append(DoctorCheck("Codex config", "OK", f"active shim model: {current}"))
    else:
        checks.append(DoctorCheck("Codex config", "INFO", "active shim model: none"))
    return checks


def _print_doctor_report(checks: list[DoctorCheck]) -> None:
    current_section = None
    for check in checks:
        if check.section != current_section:
            if current_section is not None:
                print()
            print(check.section)
            current_section = check.section
        print(f"  {check.status:<5} {check.message}")
        if check.detail:
            for line in check.detail.splitlines():
                print(f"        {line}")
    counts = Counter(check.status for check in checks)
    summary_status = "FAIL" if counts["FAIL"] else "OK"
    print()
    print("Summary")
    print(
        f"  {summary_status:<5} "
        f"{counts['OK']} ok, {counts['WARN']} warn, {counts['FAIL']} fail, {counts['INFO']} info"
    )


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _bool_text(value) -> str:
    return "true" if bool(value) else "false"


def generate(settings_path: Path, port: int) -> None:
    models = _load_models(settings_path)
    try:
        default_model_slug(models)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    router_config = router_module.load_router_config(Path(settings_path).expanduser())
    write_catalog(models, CATALOG_PATH, router_config=router_config)
    write_config(models, CONFIG_PATH, CATALOG_PATH, port)
    print(f"Generated {len(models)} model entries:")
    if _active_router(models, settings_path) is not None:
        print(f"  auto router: {router_config.slug} ({router_config.display_name})")
    print(f"  catalog: {CATALOG_PATH}")
    print(f"  config:  {CONFIG_PATH}")
    print("No files under ~/.codex were modified.")


def refresh_opencode_go(settings_path: Path, api_key_env: str, base_url: str, prefer: str, timeout: float) -> int:
    print(f"Refreshing OpenCode Go models from {base_url}...")
    try:
        result = refresh_opencode_go_settings(
            settings_path,
            api_key_env=api_key_env,
            base_url=base_url,
            prefer=prefer,
            timeout=timeout,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Refreshed {len(result.models)} OpenCode Go models into {result.settings_path}.")
    if result.skipped:
        print(f"Skipped {len(result.skipped)} models with no working probed endpoint:")
        for model_id, chat_status, messages_status in result.skipped:
            print(f"  {model_id}: chat={chat_status}, messages={messages_status}")
    for row in result.models:
        print(f"  {row['slug']}  ->  {row['model']} ({row['provider']}, {row['opencode_go_endpoint']})")
    return 0


def install_codex_config(settings_path: Path, port: int, model_slug: str | None = None) -> None:
    models = _load_models(settings_path)
    router_config = _active_router(models, settings_path)
    default_slug = _resolve_model_slug(models, model_slug, router_config)
    CODEX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    original = CODEX_CONFIG_PATH.read_text() if CODEX_CONFIG_PATH.exists() else ""
    cleaned = _remove_managed_config(original)
    current_top_level = _extract_top_level_key_lines(cleaned, MANAGED_TOP_LEVEL_KEYS)
    if current_top_level:
        previous_top_level = current_top_level
    else:
        previous_top_level = _managed_previous_top_level(original)
    if not previous_top_level and CODEX_CONFIG_BACKUP_PATH.exists():
        previous_top_level = _extract_top_level_key_lines(CODEX_CONFIG_BACKUP_PATH.read_text(), MANAGED_TOP_LEVEL_KEYS)
    cleaned = _remove_top_level_keys(cleaned, MANAGED_TOP_LEVEL_KEYS)
    cleaned = _remove_section(cleaned, f"model_providers.{PROVIDER_NAME}")
    provider_name = _provider_display_name(models, default_slug, router_config)
    top_block, provider_block = _managed_config_blocks(
        default_slug, port, previous_top_level, provider_name=provider_name
    )
    CODEX_CONFIG_PATH.write_text(top_block + "\n" + cleaned.lstrip() + "\n" + provider_block)
    print(f"Installed shim config into {CODEX_CONFIG_PATH}.")


def list_models(settings_path: Path) -> int:
    models = _load_models(settings_path)
    rows: list[tuple[str, str, str, str]] = []
    router_config = _active_router(models, settings_path)
    if router_config is not None:
        rows.append((router_config.slug, router_config.display_name, "per-task pick", "auto"))
    if chatgpt_passthrough_available():
        for slug, display_name in chatgpt_passthrough_display_names().items():
            rows.append((slug, display_name, slug, "chatgpt"))
    if cursor_passthrough_available():
        for slug, display_name in cursor_passthrough_display_names().items():
            rows.append((slug, display_name, "composer-2.5", "cursor-subscription"))
    rows.extend((model.slug, model.display_name, model.model, model.provider) for model in usable_byok_models(models))
    for model in models:
        if model not in usable_byok_models(models):
            rows.append((model.slug, f"{model.display_name} (missing API key)", model.model, model.provider))
    if not rows:
        print(
            "No models available. Create ~/.codex-shim/models.json, pass --settings /path/to/models.json, "
            "run `codex login` for GPT passthrough, or run `cursor-agent login` for Composer passthrough.",
            file=sys.stderr,
        )
        return 1
    width = max(len(row[0]) for row in rows)
    for slug, display_name, model, provider in rows:
        print(f"{slug:<{width}}  {display_name}  ->  {model} ({provider})", flush=True)
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
    process = _popen_daemon(cmd, log, env)
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
    _terminate_pid(pid)
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
        if not previous_top_level and CODEX_CONFIG_BACKUP_PATH.exists():
            previous_top_level = _extract_top_level_key_lines(CODEX_CONFIG_BACKUP_PATH.read_text(), MANAGED_TOP_LEVEL_KEYS)
        restored = _remove_managed_config(current)
        restored = _remove_section(restored, f"model_providers.{PROVIDER_NAME}")
        restored = _restore_missing_top_level_keys(restored.lstrip(), previous_top_level)
        CODEX_CONFIG_PATH.write_text(restored)
        print(f"Removed shim config from {CODEX_CONFIG_PATH}.")
    if CODEX_CONFIG_BACKUP_PATH.exists():
        CODEX_CONFIG_BACKUP_PATH.unlink()
        print(f"Removed stale shim backup {CODEX_CONFIG_BACKUP_PATH}.")


def status(port: int) -> int:
    pid = _read_pid()
    if _pid_running(pid):
        health = _health(port)
        if health is not None:
            model_count = health.get("models", "unknown")
            print(f"Shim is running on http://{DEFAULT_HOST}:{port} with pid {pid} ({model_count} models).")
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
    env = _with_loopback_no_proxy(os.environ.copy())
    if os.name == "nt":
        raise SystemExit(subprocess.call(args, env=env))
    os.execvpe("codex", args, env)


def exec_codex_app(settings_path: Path, port: int, path: str) -> None:
    _quit_codex_app()
    codex_app = patched_codex_app_bundle()
    if codex_app is not None:
        subprocess.Popen(["open", "-a", str(codex_app)], env=_with_loopback_no_proxy(os.environ.copy()))
    else:
        args = ["codex", "app", path]
        subprocess.Popen(args, env=_with_loopback_no_proxy(os.environ.copy()))
    _foreground_codex_app()


def _with_loopback_no_proxy(env: dict[str, str]) -> dict[str, str]:
    loopback = ["127.0.0.1", "localhost", "::1"]
    for key in ("NO_PROXY", "no_proxy"):
        values = [part.strip() for part in env.get(key, "").split(",") if part.strip()]
        lower_values = {value.lower() for value in values}
        for host in loopback:
            if host.lower() not in lower_values:
                values.append(host)
        env[key] = ",".join(values)
    return env


def _quit_codex_app() -> None:
    script = 'tell application "Codex" to if it is running then quit'
    try:
        subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
    except OSError:
        pass


def _asar_package_spec() -> str:
    """The pinned `name@version` ASAR package spec used for patching.

    Defaults to ``DEFAULT_ASAR_PACKAGE``; can be overridden via the
    ``CODEX_SHIM_ASAR_PACKAGE`` environment variable for mirrors or upgrades.
    """
    return os.environ.get(ASAR_PACKAGE_ENV, "").strip() or DEFAULT_ASAR_PACKAGE


def _asar_command(*args: str) -> list[str]:
    """Build a pinned ``npx`` invocation of the ASAR packer.

    ``--package <pinned spec>`` forces npx to resolve and run the exact audited
    release (``DEFAULT_ASAR_PACKAGE``) regardless of what is on ``PATH`` or in the
    cache, instead of an unpinned ``npx --yes asar`` that runs whatever the
    registry currently serves. ``--yes`` only auto-confirms that pinned package.
    """
    spec = _asar_package_spec()
    return ["npx", "--yes", "--package", spec, "asar", *args]


def patch_codex_app() -> int:
    if sys.platform != "darwin":
        print("patch-app is macOS-only; Windows MSIX Codex Desktop cannot be patched with this ASAR helper.", file=sys.stderr)
        return 1
    codex_app = _codex_app_bundle_for_patch()
    app_asar = codex_app / "Contents/Resources/app.asar"
    info_plist = codex_app / "Contents/Info.plist"
    backup = RUNTIME_DIR / APP_ASAR_BACKUP_NAME
    info_backup = RUNTIME_DIR / INFO_PLIST_BACKUP_NAME

    if not app_asar.exists():
        print(f"Codex app bundle not found at {codex_app}.", file=sys.stderr)
        return 1
    if codex_app == USER_CODEX_APP:
        print(f"Patching user Codex copy at {codex_app}.")
    if not info_plist.exists():
        print(f"Codex Info.plist not found at {info_plist}.", file=sys.stderr)
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
    if not info_backup.exists():
        info_backup.write_bytes(info_plist.read_bytes())
        print(f"Backed up original Info.plist to {info_backup}.")

    _quit_codex_app()
    workdir = RUNTIME_DIR / "app-asar-work-user"
    if workdir.exists():
        import shutil

        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    print(f"Using pinned ASAR packer {_asar_package_spec()} (override with {ASAR_PACKAGE_ENV}).")
    subprocess.run(_asar_command("extract", str(app_asar), str(workdir)), check=True)
    changed = _patch_codex_desktop_bundles(workdir)
    if changed is None:
        return 1
    if changed:
        subprocess.run(_asar_command("pack", str(workdir), str(app_asar)), check=True)
        _update_app_asar_integrity(app_asar, info_plist)
        _resign_codex_app(codex_app)
    return 0


def restore_codex_app_bundle() -> int:
    if sys.platform != "darwin":
        print("restore-app is macOS-only; Windows MSIX Codex Desktop cannot be restored with this ASAR helper.", file=sys.stderr)
        return 1
    codex_app = patched_codex_app_bundle() or _codex_app_bundle_for_patch()
    app_asar = codex_app / "Contents/Resources/app.asar"
    info_plist = codex_app / "Contents/Info.plist"
    backup = RUNTIME_DIR / APP_ASAR_BACKUP_NAME
    info_backup = RUNTIME_DIR / INFO_PLIST_BACKUP_NAME
    if not backup.exists():
        print(f"No app.asar backup found at {backup}.")
        return 0
    _quit_codex_app()
    app_asar.write_bytes(backup.read_bytes())
    if info_backup.exists():
        info_plist.write_bytes(info_backup.read_bytes())
        print(f"Restored {info_plist} from {info_backup}.")
    elif info_plist.exists():
        _update_app_asar_integrity(app_asar, info_plist)
    _resign_codex_app(codex_app)
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


def _app_asar_header_hash(path: Path) -> str:
    with path.open("rb") as f:
        _, _, _, json_size = struct.unpack("<4I", f.read(16))
        header_json = f.read(json_size)
    return hashlib.sha256(header_json).hexdigest()


def _update_app_asar_integrity(app_asar: Path, info_plist: Path) -> None:
    header_hash = _app_asar_header_hash(app_asar)
    data = plistlib.loads(info_plist.read_bytes())
    try:
        data["ElectronAsarIntegrity"]["Resources/app.asar"]["hash"] = header_hash
    except KeyError as exc:
        raise RuntimeError(f"Could not update ElectronAsarIntegrity in {info_plist}") from exc
    info_plist.write_bytes(plistlib.dumps(data))
    print("Updated ElectronAsarIntegrity for app.asar.")


def _patch_codex_desktop_bundles(workdir: Path) -> bool | None:
    patches = [
        (
            "model picker allowlist filter",
            [
                "models-and-reasoning-efforts-*.js",
                "model-queries-*.js",
                "*.js",
            ],
            MODEL_PICKER_NEEDLE,
            MODEL_PICKER_REPLACEMENT,
            MODEL_PICKER_APPLIED,
        ),
        (
            "shim-mode sidebar provider filter",
            ["app-server-manager-signals-*.js", "*.js"],
            SIDEBAR_RECENT_THREADS_NEEDLE,
            SIDEBAR_RECENT_THREADS_REPLACEMENT,
            SIDEBAR_RECENT_THREADS_APPLIED,
        ),
    ]
    changed = False
    for label, globs, needle, replacement, applied in patches:
        bundle_file = _find_js_bundle(workdir, globs, needle, applied)
        if bundle_file is None:
            print(f"Could not find the expected {label} in Codex Desktop.", file=sys.stderr)
            return None
        result = _replace_once(bundle_file, needle, replacement, applied)
        if result is None:
            print(f"Could not patch the expected {label} in Codex Desktop.", file=sys.stderr)
            return None
        if result:
            changed = True
            print(f"Patched Codex Desktop {label}.")
        else:
            print(f"Codex Desktop {label} patch is already applied.")
    return changed


def _find_js_bundle(
    workdir: Path,
    globs: list[str],
    needle: re.Pattern[str],
    applied: re.Pattern[str],
) -> Path | None:
    assets_dir = workdir / "webview" / "assets"
    if not assets_dir.exists():
        return None
    candidates: list[Path] = []
    for pattern in globs:
        candidates.extend(p for p in sorted(assets_dir.glob(pattern)) if p not in candidates)
    for path in candidates:
        text = _read_text_lossy(path)
        if needle.search(text) or applied.search(text):
            return path
    return None


def _replace_once(
    path: Path,
    needle: re.Pattern[str],
    replacement: str,
    applied: re.Pattern[str],
) -> bool | None:
    text = _read_text_lossy(path)
    matches = needle.findall(text)
    if not matches:
        if applied.search(text):
            return False
        return None
    if len(matches) != 1:
        return None
    path.write_text(needle.sub(replacement, text, count=1))
    return True


def _read_text_lossy(path: Path) -> str:
    try:
        return path.read_text()
    except UnicodeDecodeError:
        return path.read_text(errors="ignore")


def patched_codex_app_bundle() -> Path | None:
    for codex_app in (USER_CODEX_APP, SYSTEM_CODEX_APP):
        app_asar = codex_app / "Contents/Resources/app.asar"
        if app_asar.exists() and _app_asar_is_patched(app_asar):
            return codex_app
    return None


def _codex_app_bundle_for_patch() -> Path:
    system_asar = SYSTEM_CODEX_APP / "Contents/Resources/app.asar"
    if system_asar.exists() and _path_is_writable(system_asar):
        return SYSTEM_CODEX_APP
    return _ensure_user_codex_app()


def _ensure_user_codex_app() -> Path:
    user_asar = USER_CODEX_APP / "Contents/Resources/app.asar"
    if user_asar.exists():
        return USER_CODEX_APP
    system_asar = SYSTEM_CODEX_APP / "Contents/Resources/app.asar"
    if not system_asar.exists():
        raise SystemExit(f"Codex Desktop not found at {SYSTEM_CODEX_APP}.")
    USER_CODEX_APP.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ditto", str(SYSTEM_CODEX_APP), str(USER_CODEX_APP)], check=True)
    print(f"Copied Codex Desktop to {USER_CODEX_APP} for patching.")
    return USER_CODEX_APP


def _path_is_writable(path: Path) -> bool:
    try:
        with path.open("r+b"):
            return True
    except OSError:
        return False


def _app_asar_is_patched(app_asar: Path) -> bool:
    try:
        text = app_asar.read_bytes().decode("utf-8", errors="ignore")
    except OSError:
        return False
    return MODEL_PICKER_APPLIED.search(text) is not None and SIDEBAR_RECENT_THREADS_APPLIED.search(text) is not None


def _resign_codex_app(codex_app: Path = SYSTEM_CODEX_APP) -> None:
    # Electron validates app.asar through the bundle signature metadata at
    # startup. Re-sign after patching so the modified archive does not trip the
    # asar integrity check.
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(codex_app)],
        check=True,
    )
    print(f"Re-signed {codex_app} after patch.")


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


def _provider_display_name(models, slug: str, router_config=None) -> str:
    if router_config is not None and slug == router_config.slug:
        return router_config.display_name
    if chatgpt_passthrough_available():
        display_name = chatgpt_passthrough_display_names().get(slug)
        if display_name:
            return display_name
    if cursor_passthrough_available():
        display_name = cursor_passthrough_display_names().get(slug)
        if display_name:
            return display_name
    for model in models:
        if model.slug == slug:
            return model.display_name
    return "Codex Shim"


def _managed_config_blocks(
    default_slug: str,
    port: int,
    previous_top_level: dict[str, str] | None = None,
    provider_name: str = "Codex Shim",
) -> tuple[str, str]:
    metadata = ""
    if previous_top_level:
        metadata = PREVIOUS_TOP_LEVEL_PREFIX + json.dumps(previous_top_level, sort_keys=True) + "\n"
    top_block = f'''{MANAGED_BEGIN}
{metadata}model = "{_toml_escape(default_slug)}"
model_provider = "{PROVIDER_NAME}"
model_catalog_json = "{_toml_escape(str(CATALOG_PATH))}"
{MANAGED_END}
'''

    provider_block = f'''{MANAGED_BEGIN}
[model_providers.{PROVIDER_NAME}]
name = "{_toml_escape(provider_name)}"
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


def _popen_daemon(cmd: list[str], log, env: dict[str, str]) -> subprocess.Popen:
    kwargs = {"cwd": str(PROJECT_ROOT), "env": env, "stdout": log, "stderr": log}
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        return subprocess.Popen(cmd, creationflags=creationflags, **kwargs)
    return subprocess.Popen(cmd, start_new_session=True, **kwargs)


def _terminate_pid(pid: int) -> None:
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(WINDOWS_PROCESS_TERMINATE, False, pid)
        if handle:
            try:
                ctypes.windll.kernel32.TerminateProcess(handle, 0)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        return
    os.kill(pid, signal.SIGTERM)


def _override_args(settings_path: Path, port: int) -> list[str]:
    models = _load_models(settings_path)
    try:
        default_slug = default_model_slug(models)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    pairs = codex_config_overrides(CATALOG_PATH, default_slug, port)
    args: list[str] = []
    for pair in pairs:
        args.extend(["-c", pair])
    return args


def _resolve_model_slug(models, requested: str | None, router_config=None) -> str:
    if requested is None:
        current = _current_managed_model()
        if current in _valid_model_slugs(models, router_config):
            return current
        try:
            return default_model_slug(models)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if router_config is not None and requested == router_config.slug:
        return requested
    if is_chatgpt_passthrough_slug(requested):
        if not chatgpt_passthrough_available():
            raise SystemExit(
                "ChatGPT passthrough requires a Codex login. "
                "Run `codex login` so ~/.codex/auth.json contains tokens.access_token."
            )
        if requested.startswith("openai-gpt-"):
            return CHATGPT_MODEL_SLUG
        return requested
    if is_cursor_passthrough_slug(requested):
        if not cursor_passthrough_available():
            raise SystemExit(
                "Composer passthrough requires Cursor CLI login. "
                "Run `cursor-agent login`, then `cursor-agent status`."
            )
        return requested if requested in cursor_passthrough_display_names() else "composer-2-5"
    by_slug = {model.slug: model.slug for model in models}
    by_model: dict[str, list[str]] = {}
    for model in models:
        by_model.setdefault(model.model, []).append(model.slug)
    if requested in by_slug:
        return requested
    configured = {model.slug: model for model in models}
    if requested in configured and not byok_model_has_credentials(configured[requested]):
        if is_cursor_passthrough_slug(requested):
            raise SystemExit(
                f"Model {requested!r} is configured for BYOK but has no API key. "
                "Remove it from ~/.codex-shim/models.json to use Cursor subscription passthrough, "
                "or set CURSOR_API_KEY / ~/.codex-shim/cursor-api-key."
            )
        raise SystemExit(
            f"Model {requested!r} is configured but has no API key. "
            "Set the provider API key in ~/.codex-shim/models.json or the matching env var."
        )
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


def _valid_model_slugs(models, router_config=None) -> set[str]:
    slugs = {model.slug for model in usable_byok_models(models)}
    if router_config is not None:
        slugs.add(router_config.slug)
    if chatgpt_passthrough_available():
        slugs.update(chatgpt_passthrough_slugs())
    if cursor_passthrough_available():
        slugs.update(cursor_passthrough_display_names())
    return slugs


def _healthy(port: int) -> bool:
    return _health(port) is not None


def _health(port: int) -> dict | None:
    try:
        with urlopen(f"http://{DEFAULT_HOST}:{port}/health", timeout=0.5) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def _read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text().strip())
    except Exception:
        return None


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == WINDOWS_STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _entrypoint() -> int:
    try:
        return main()
    except BrokenPipeError:
        # Downstream pipe (e.g. `codex-shim list | head`) closed early. Mute the
        # interpreter's atexit flush so we exit cleanly instead of dumping a
        # traceback to stderr.
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            pass
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
