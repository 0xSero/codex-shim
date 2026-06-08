from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any


DEFAULT_SETTINGS = Path.home() / ".codex-shim" / "models.json"
DEFAULT_CURSOR_API_KEY_FILE = Path.home() / ".codex-shim" / "cursor-api-key"
DEFAULT_CODEX_AUTH = Path.home() / ".codex" / "auth.json"
DEFAULT_CODEX_MODELS_CACHE = Path.home() / ".codex" / "models_cache.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PROVIDER_NAME = "codex_shim"
CHATGPT_MODEL_SLUG = "gpt-5.5"
FALLBACK_CHATGPT_PASSTHROUGH_SLUGS = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.2",
    "codex-auto-review",
)
FALLBACK_CHATGPT_DISPLAY_NAMES = {
    "gpt-5.5": "GPT-5.5",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.4-mini": "GPT-5.4-Mini",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.3-codex-spark": "GPT-5.3-Codex-Spark",
    "gpt-5.2": "gpt-5.2",
    "codex-auto-review": "Codex Auto Review",
}


def chatgpt_passthrough_available(auth_path: Path | None = None) -> bool:
    """Return True if ~/.codex/auth.json holds a usable Codex access token."""
    if os.environ.get("CODEX_SHIM_DISABLE_CHATGPT", "").lower() in {"1", "true", "yes", "on"}:
        return False
    if auth_path is None:
        import sys as _sys

        auth_path = getattr(_sys.modules[__name__], "DEFAULT_CODEX_AUTH")
    expanded = Path(auth_path).expanduser()
    if not expanded.exists():
        return False
    try:
        data = json.loads(expanded.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return False
    return bool(tokens.get("access_token"))


def _is_listed_gpt_model(entry: dict[str, Any]) -> bool:
    slug = str(entry.get("slug") or "").strip()
    if not slug:
        return False
    if entry.get("visibility") == "hidden":
        return False
    lower = slug.lower()
    return lower.startswith("gpt-") or lower.startswith("codex-")


def _minimal_chatgpt_passthrough_entry(slug: str, display_name: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "display_name": display_name,
        "description": f"OpenAI {display_name} routed through ChatGPT passthrough.",
        "context_window": 400000,
        "max_context_window": 400000,
        "auto_compact_token_limit": 320000,
        "truncation_policy": {"mode": "tokens", "limit": 64000},
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": "minimal", "description": "Minimal reasoning"},
            {"effort": "low", "description": "Faster, lighter reasoning"},
            {"effort": "medium", "description": "Balanced"},
            {"effort": "high", "description": "Deeper reasoning"},
            {"effort": "xhigh", "description": "Maximum reasoning"},
        ],
        "default_reasoning_summary": "auto",
        "reasoning_summary_format": "experimental",
        "supports_reasoning_summaries": True,
        "default_verbosity": "medium",
        "support_verbosity": True,
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        "supports_search_tool": True,
        "supports_parallel_tool_calls": True,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
        "supports_image_detail_original": True,
        "shell_type": "shell_command",
        "visibility": "list",
        "minimal_client_version": "0.0.1",
        "supported_in_api": True,
        "availability_nux": None,
        "upgrade": None,
        "priority": 10000 if slug == CHATGPT_MODEL_SLUG else 9000,
        "prefer_websockets": False,
        "available_in_plans": ["free", "plus", "pro", "team", "business", "enterprise"],
        "base_instructions": f"You are Codex, a coding agent powered by {display_name}.",
        "model_messages": {
            "instructions_template": f"You are Codex, a coding agent powered by {display_name}.",
            "instructions_variables": {"model_name": display_name},
        },
        **({"isDefault": True} if slug == CHATGPT_MODEL_SLUG else {}),
    }


def load_chatgpt_passthrough_catalog_models(cache_path: Path | None = None) -> list[dict[str, Any]]:
    path = Path(cache_path or DEFAULT_CODEX_MODELS_CACHE).expanduser()
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            models = data.get("models")
            if isinstance(models, list):
                entries = [dict(model) for model in models if isinstance(model, dict) and _is_listed_gpt_model(model)]
                if entries:
                    return entries
    return [
        _minimal_chatgpt_passthrough_entry(
            slug,
            FALLBACK_CHATGPT_DISPLAY_NAMES.get(slug, slug),
        )
        for slug in FALLBACK_CHATGPT_PASSTHROUGH_SLUGS
    ]


def chatgpt_passthrough_slugs(cache_path: Path | None = None) -> set[str]:
    return {str(model["slug"]) for model in load_chatgpt_passthrough_catalog_models(cache_path) if model.get("slug")}


def chatgpt_passthrough_display_names(cache_path: Path | None = None) -> dict[str, str]:
    return {
        str(model["slug"]): str(model.get("display_name") or model["slug"])
        for model in load_chatgpt_passthrough_catalog_models(cache_path)
        if model.get("slug")
    }


def is_chatgpt_passthrough_slug(slug: str, cache_path: Path | None = None) -> bool:
    if slug.startswith("openai-gpt-"):
        return True
    return slug in chatgpt_passthrough_slugs(cache_path)


def chatgpt_upstream_model(slug: str, cache_path: Path | None = None) -> str:
    if slug.startswith("openai-gpt-"):
        return CHATGPT_MODEL_SLUG
    if slug in chatgpt_passthrough_slugs(cache_path):
        return slug
    return CHATGPT_MODEL_SLUG


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "model"


@dataclass(frozen=True)
class ShimModel:
    slug: str
    model: str
    display_name: str
    provider: str
    base_url: str
    api_key: str = ""
    index: int = 0
    max_context_limit: int | None = None
    max_output_tokens: int | None = None
    no_image_support: bool = False
    extra_headers: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_anthropic(self) -> bool:
        return self.provider == "anthropic"

    @property
    def is_openai_chat(self) -> bool:
        return self.provider in {"openai", "generic-chat-completion-api"}


class ModelSettings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or DEFAULT_SETTINGS).expanduser()

    def load(self) -> list[ShimModel]:
        if not self.path.exists():
            if self.path == DEFAULT_SETTINGS:
                return []
            raise FileNotFoundError(self.path)
        data = json.loads(self.path.read_text())
        rows = _model_rows(data)
        model_counts: dict[str, int] = {}
        for row in rows:
            model = str(row.get("model") or "").strip()
            if model:
                model_counts[model] = model_counts.get(model, 0) + 1

        used: set[str] = set()
        models: list[ShimModel] = []
        for fallback_index, row in enumerate(rows):
            model = str(row.get("model") or "").strip()
            provider = str(row.get("provider") or "").strip()
            base_url = str(_field(row, "base_url", "baseUrl") or "").strip().rstrip("/")
            if not model or not provider or not base_url:
                continue

            index = int(row.get("index", fallback_index))
            display_name = str(_field(row, "display_name", "displayName", default=model)).strip()
            slug_base = str(row.get("slug") or (display_name if model_counts.get(model, 0) > 1 else model))
            slug = slugify(slug_base)
            if slug in used:
                slug = f"{slug}-{index}"
            while slug in used:
                slug = f"{slug}-{len(used)}"
            used.add(slug)

            extra_headers = {
                str(k): str(v)
                for k, v in (_field(row, "extra_headers", "extraHeaders", default={}) or {}).items()
                if v is not None
            }
            api_key_env = str(_field(row, "api_key_env", "apiKeyEnv", default="")).strip()
            api_key = str(_field(row, "api_key", "apiKey", default=""))
            if api_key_env:
                api_key = os.environ.get(api_key_env, api_key).strip()
            else:
                api_key = _resolve_api_key(api_key)
            models.append(
                ShimModel(
                    slug=slug,
                    model=model,
                    display_name=display_name,
                    provider=provider,
                    base_url=base_url,
                    api_key=api_key,
                    index=index,
                    max_context_limit=_int_or_none(_field(row, "max_context_limit", "maxContextLimit")),
                    max_output_tokens=_int_or_none(_field(row, "max_output_tokens", "maxOutputTokens")),
                    no_image_support=bool(_field(row, "no_image_support", "noImageSupport", default=False)),
                    extra_headers=extra_headers,
                    raw=row,
                )
            )
        return models

    def by_slug_or_model(self, requested: str) -> ShimModel | None:
        models = self.load()
        by_slug = {m.slug: m for m in models}
        if requested in by_slug:
            return by_slug[requested]
        matches = [m for m in models if m.model == requested]
        if len(matches) == 1:
            return matches[0]
        return None

    def load_router(self):
        """Parse the optional top-level ``router`` block from the settings file."""
        from .router import load_router_config

        return load_router_config(self.path)


def _model_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("models")
        if rows is None:
            rows = data.get("customModels")
        if rows is None:
            rows = data.get("launchModels", data.get("launch_models", []))
    else:
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in (_coerce_model_row(row) for row in rows) if row is not None]


def _coerce_model_row(row: Any) -> dict[str, Any] | None:
    if isinstance(row, str):
        return {
            "model": row,
            "display_name": row,
            "provider": "generic-chat-completion-api",
            "base_url": "http://127.0.0.1:11434/v1",
        }
    if isinstance(row, dict):
        return _normalize_model_row(row)
    return None


def _normalize_model_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    if "display_name" not in normalized and "name" in normalized:
        normalized["display_name"] = normalized["name"]
    if "base_url" not in normalized and "baseURL" in normalized:
        normalized["base_url"] = normalized["baseURL"]
    if "api_key" not in normalized and "apiKey" not in normalized and "bearerToken" in normalized:
        normalized["api_key"] = normalized["bearerToken"]
    if _looks_like_ollama_row(normalized):
        normalized["provider"] = "generic-chat-completion-api"
        if not _field(normalized, "base_url", "baseUrl", "baseURL"):
            normalized["base_url"] = "http://127.0.0.1:11434/v1"
    return normalized


def _looks_like_ollama_row(row: dict[str, Any]) -> bool:
    provider = str(row.get("provider") or "").lower()
    base_url = str(_field(row, "base_url", "baseUrl", "baseURL", default="")).lower()
    return provider == "ollama" or "11434" in base_url or "ollama" in base_url


def _field(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return default


def _resolve_api_key(value: str) -> str:
    raw = value.strip()
    if raw.startswith("${") and raw.endswith("}"):
        raw = os.environ.get(raw[2:-1].strip(), "")
    if not raw and DEFAULT_CURSOR_API_KEY_FILE.exists():
        try:
            raw = DEFAULT_CURSOR_API_KEY_FILE.read_text().strip()
        except OSError:
            raw = ""
    if not raw:
        raw = os.environ.get("CURSOR_API_KEY", "").strip()
    return raw


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def default_model_slug(models: list[ShimModel], include_chatgpt: bool | None = None) -> str:
    from .cursor_passthrough import CURSOR_MODEL_SLUG, cursor_passthrough_available

    if include_chatgpt is None:
        include_chatgpt = chatgpt_passthrough_available()
    if include_chatgpt:
        return CHATGPT_MODEL_SLUG
    usable = usable_byok_models(models)
    if usable:
        return usable[0].slug
    if cursor_passthrough_available():
        return CURSOR_MODEL_SLUG
    raise ValueError(
        "No usable codex-shim models: add models to ~/.codex-shim/models.json, run `codex login`, "
        "run `cursor-agent login`, or unset CODEX_SHIM_DISABLE_CHATGPT / CODEX_SHIM_DISABLE_CURSOR."
    )


def usable_byok_models(models: list[ShimModel]) -> list[ShimModel]:
    return [model for model in models if byok_model_has_credentials(model)]


def available_model_slugs(models: list[ShimModel]) -> set[str]:
    """Every model slug the shim can route to right now: usable BYOK models plus
    any available ChatGPT/Cursor passthrough slugs. Used by the Auto Router to
    keep routing to candidates that actually exist."""
    from .cursor_passthrough import cursor_passthrough_available, cursor_passthrough_display_names

    slugs = {model.slug for model in usable_byok_models(models)}
    if chatgpt_passthrough_available():
        slugs |= chatgpt_passthrough_slugs()
    if cursor_passthrough_available():
        slugs |= set(cursor_passthrough_display_names())
    return slugs


def byok_model_has_credentials(model: ShimModel) -> bool:
    return bool(model.api_key.strip())
