from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .settings import slugify


OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_API_KEY_ENV = "OPENCODE_GO_API_KEY"
OPENCODE_GO_GENERATED_BY = "codex-shim opencode-go refresh"


@dataclass(frozen=True)
class OpenCodeGoRefreshResult:
    settings_path: Path
    models: list[dict[str, Any]]
    skipped: list[tuple[str, int, int]]


def refresh_opencode_go_settings(
    settings_path: Path,
    *,
    api_key_env: str = OPENCODE_GO_API_KEY_ENV,
    base_url: str = OPENCODE_GO_BASE_URL,
    prefer: str = "chat",
    timeout: float = 30.0,
) -> OpenCodeGoRefreshResult:
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"Set {api_key_env} before refreshing OpenCode Go models.")

    base_url = base_url.rstrip("/")
    model_ids = fetch_opencode_go_model_ids(base_url, api_key, timeout=timeout)
    generated: list[dict[str, Any]] = []
    skipped: list[tuple[str, int, int]] = []
    for model_id in model_ids:
        chat_status = probe_chat_model(base_url, api_key, model_id, timeout=timeout)
        messages_status = probe_messages_model(base_url, api_key, model_id, timeout=timeout)
        row = opencode_go_model_row(
            model_id,
            chat_status=chat_status,
            messages_status=messages_status,
            api_key_env=api_key_env,
            base_url=base_url,
            prefer=prefer,
        )
        if row is None:
            skipped.append((model_id, chat_status, messages_status))
        else:
            generated.append(row)

    write_opencode_go_models(settings_path, generated, base_url=base_url)
    return OpenCodeGoRefreshResult(settings_path=Path(settings_path).expanduser(), models=generated, skipped=skipped)


def fetch_opencode_go_model_ids(base_url: str, api_key: str, *, timeout: float = 30.0) -> list[str]:
    status, payload = _request_json(
        "GET",
        f"{base_url.rstrip('/')}/models",
        {"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"OpenCode Go /models returned HTTP {status}.")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("OpenCode Go /models did not return a model list.")
    ids = [str(item.get("id") or "").strip() for item in data if isinstance(item, dict)]
    return [model_id for model_id in ids if model_id]


def probe_chat_model(base_url: str, api_key: str, model_id: str, *, timeout: float = 30.0) -> int:
    status, _payload = _request_json(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        {
            "model": model_id,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        },
        timeout=timeout,
    )
    return status


def probe_messages_model(base_url: str, api_key: str, model_id: str, *, timeout: float = 30.0) -> int:
    status, _payload = _request_json(
        "POST",
        f"{base_url.rstrip('/')}/messages",
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        {
            "model": model_id,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        },
        timeout=timeout,
    )
    return status


def opencode_go_model_row(
    model_id: str,
    *,
    chat_status: int,
    messages_status: int,
    api_key_env: str,
    base_url: str,
    prefer: str,
) -> dict[str, Any] | None:
    chat_ok = 200 <= chat_status < 300
    messages_ok = 200 <= messages_status < 300
    if chat_ok and (prefer == "chat" or not messages_ok):
        provider = "generic-chat-completion-api"
        endpoint = "chat"
    elif messages_ok:
        provider = "anthropic"
        endpoint = "messages"
    else:
        return None
    return {
        "slug": f"ocgo-{slugify(model_id)}",
        "model": model_id,
        "display_name": f"OpenCode Go {display_name_from_model_id(model_id)}",
        "provider": provider,
        "base_url": base_url.rstrip("/"),
        "api_key_env": api_key_env,
        "no_image_support": True,
        "generated_by": OPENCODE_GO_GENERATED_BY,
        "opencode_go_endpoint": endpoint,
    }


def write_opencode_go_models(settings_path: Path, rows: list[dict[str, Any]], *, base_url: str = OPENCODE_GO_BASE_URL) -> None:
    path = Path(settings_path).expanduser()
    existing = _read_settings_json(path)
    payload = dict(existing) if isinstance(existing, dict) else {}
    target = next((k for k in ("models", "customModels", "launchModels", "launch_models") if k in payload), "models")
    preserved = [row for row in _settings_rows(existing) if not _is_opencode_go_row(row, base_url)]
    payload[target] = [*preserved, *rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def display_name_from_model_id(model_id: str) -> str:
    aliases = {
        "glm": "GLM",
        "kimi": "Kimi",
        "deepseek": "DeepSeek",
        "qwen": "Qwen",
        "minimax": "MiniMax",
        "mimo": "MiMo",
    }
    parts = model_id.replace("-", " ").split()
    titled = []
    for part in parts:
        lower = part.lower()
        if lower in aliases:
            titled.append(aliases[lower])
        elif lower.startswith("qwen"):
            titled.append("Qwen" + part[4:])
        elif part[0].isalpha() and len(part) > 1 and part[1:].isdigit():
            titled.append(part[0].upper() + part[1:])
        else:
            titled.append(part.capitalize())
    return " ".join(titled)


def _request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"User-Agent": "codex-shim", "Accept": "application/json", **headers}
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return int(response.status), _json_payload(raw)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), _json_payload(raw)
    except URLError as exc:
        raise RuntimeError(f"Could not reach OpenCode Go: {exc.reason}") from exc


def _json_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_settings_json(path: Path) -> Any:
    expanded = Path(path).expanduser()
    if not expanded.exists():
        return {}
    try:
        return json.loads(expanded.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Settings file is not valid JSON: {expanded}: {exc}") from exc


def _settings_rows(data: Any) -> list[dict[str, Any]]:
    rows: Any
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("models")
        if rows is None:
            rows = data.get("customModels")
        if rows is None:
            rows = data.get("launchModels", data.get("launch_models", []))
    else:
        rows = []
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _is_opencode_go_row(row: dict[str, Any], base_url: str) -> bool:
    return row.get("generated_by") == OPENCODE_GO_GENERATED_BY
