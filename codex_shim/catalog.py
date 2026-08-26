from __future__ import annotations

import json
from pathlib import Path

from . import router as router_module
from .settings import (
    DEFAULT_PORT,
    CHATGPT_MODEL_SLUG,
    PROVIDER_NAME,
    ShimModel,
    available_model_slugs,
    chatgpt_passthrough_available,
    chatgpt_passthrough_display_names,
    default_model_slug,
    load_chatgpt_passthrough_catalog_models,
    usable_byok_models,
)
from .cursor_passthrough import (
    cursor_catalog_entry,
    cursor_passthrough_available,
    cursor_passthrough_display_names,
)


PLAN_TIERS = ["free", "plus", "pro", "team", "business", "enterprise"]


def catalog_entry(model: ShimModel) -> dict:
    context = model.max_context_limit or _default_context(model)
    compact = max(8_000, int(context * 0.8))
    truncation = min(64_000, max(8_000, int(context * 0.32)))
    reasoning = _reasoning_effort(model)
    return {
        "slug": model.slug,
        "display_name": model.display_name,
        "description": f"{model.display_name} via local Codex shim.",
        "context_window": context,
        "max_context_window": context,
        "auto_compact_token_limit": compact,
        "truncation_policy": {"mode": "tokens", "limit": truncation},
        "default_reasoning_level": reasoning,
        "supported_reasoning_levels": [
            {"effort": "low", "description": "Faster, lighter reasoning"},
            {"effort": "medium", "description": "Balanced speed and reasoning"},
            {"effort": "high", "description": "Deeper reasoning"},
            {"effort": "xhigh", "description": "Maximum reasoning where supported"},
        ],
        "default_reasoning_summary": "none",
        "reasoning_summary_format": "none",
        "supports_reasoning_summaries": False,
        "default_verbosity": "low",
        "support_verbosity": False,
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        "supports_search_tool": False,
        "supports_parallel_tool_calls": True,
        "experimental_supported_tools": [],
        "input_modalities": ["text"] if model.no_image_support else ["text", "image"],
        "supports_image_detail_original": not model.no_image_support,
        "shell_type": "shell_command",
        "visibility": "list",
        "minimal_client_version": "0.0.1",
        "supported_in_api": True,
        "availability_nux": None,
        "upgrade": None,
        "priority": max(1, 1000 - model.index),
        "prefer_websockets": False,
        "available_in_plans": PLAN_TIERS,
        "base_instructions": "You are a coding agent running in Codex through a local BYOK shim.",
        "model_messages": {
            "instructions_template": (
                "You are Codex running on {model_name} through a local all-model shim. "
                "Be a helpful, direct coding collaborator."
            ),
            "instructions_variables": {"model_name": model.display_name},
        },
    }


def chatgpt_passthrough_entries() -> list[dict]:
    """Catalog entries for GPT models routed through ChatGPT passthrough."""
    entries: list[dict] = []
    for raw in load_chatgpt_passthrough_catalog_models():
        entry = dict(raw)
        entry["visibility"] = "list"
        entry.setdefault("available_in_plans", PLAN_TIERS)
        entry.setdefault("minimal_client_version", "0.0.1")
        entry.setdefault("supported_in_api", True)
        if entry.get("slug") == CHATGPT_MODEL_SLUG:
            entry["isDefault"] = True
            entry["priority"] = max(int(entry.get("priority") or 0), 10000)
        entries.append(entry)
    return entries


def pi_model_catalog(
    models: list[ShimModel],
    port: int = DEFAULT_PORT,
) -> dict:
    """Build a native Pi models.json document for this shim instance."""
    response_base_url = f"http://127.0.0.1:{port}/v1"
    anthropic_base_url = response_base_url.removesuffix("/v1")

    pi_models: list[dict] = []

    if chatgpt_passthrough_available():
        for raw in load_chatgpt_passthrough_catalog_models():
            slug = str(raw.get("slug") or "").strip()
            if not slug:
                continue
            pi_models.append(
                {
                    "id": slug,
                    "name": str(raw.get("display_name") or slug),
                    "api": "openai-responses",
                    "reasoning": True,
                    "input": _pi_input(raw),
                    "contextWindow": _pi_context(raw),
                    "maxTokens": _pi_max_tokens(raw, 64_000),
                }
            )

    if cursor_passthrough_available():
        for slug, display_name in cursor_passthrough_display_names().items():
            raw = cursor_catalog_entry()
            pi_models.append(
                {
                    "id": slug,
                    "name": display_name,
                    "api": "openai-completions",
                    "reasoning": False,
                    "input": _pi_input(raw),
                    "contextWindow": _pi_context(raw),
                    "maxTokens": _pi_max_tokens(raw, 32_000),
                }
            )

    for model in models:
        if model.provider == "anthropic":
            api = "anthropic-messages"
            model_base_url = anthropic_base_url
        elif model.provider in {"chatgpt", "openai", "openai-responses"}:
            api = "openai-responses"
            model_base_url = response_base_url
        else:
            api = "openai-completions"
            model_base_url = response_base_url

        entry = {
            "id": model.slug,
            "name": model.display_name,
            "api": api,
            "reasoning": False,
            "input": ["text"] if model.no_image_support else ["text", "image"],
            "contextWindow": model.max_context_limit or _default_context(model),
            "maxTokens": model.max_output_tokens or 32_000,
        }
        if model_base_url != response_base_url:
            entry["baseUrl"] = model_base_url
        pi_models.append(entry)

    unique_models: list[dict] = []
    seen_ids: set[str] = set()
    for entry in pi_models:
        model_id = str(entry["id"])
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        unique_models.append(entry)

    return {
        "providers": {
            "codex-shim": {
                "name": "Codex Shim",
                "baseUrl": response_base_url,
                "apiKey": "dummy",
                "authHeader": True,
                "models": unique_models,
            }
        }
    }


def chatgpt_passthrough_entry() -> dict:
    """Catalog entry for the default GPT-5.5 ChatGPT passthrough model."""
    for entry in chatgpt_passthrough_entries():
        if entry.get("slug") == CHATGPT_MODEL_SLUG:
            return entry
    return chatgpt_passthrough_entries()[0]


def write_catalog(models: list[ShimModel], path: Path, router_config=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    if router_config is not None and router_module.router_is_active(router_config, available_model_slugs(models)):
        entries.append(router_module.router_catalog_entry(router_config))
    if chatgpt_passthrough_available():
        entries.extend(chatgpt_passthrough_entries())
    if cursor_passthrough_available():
        entry = cursor_catalog_entry()
        entry["isDefault"] = not chatgpt_passthrough_available()
        entries.append(entry)
    entries.extend(catalog_entry(model) for model in usable_byok_models(models))
    payload = {"models": entries}
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    return path


def write_config(models: list[ShimModel], path: Path, catalog_path: Path, port: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        default_slug = default_model_slug(models)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    text = f'''# Generated by codex-shim. This file is opt-in and is not ~/.codex/config.toml.
model = "{_toml_escape(default_slug)}"
model_provider = "{PROVIDER_NAME}"
model_catalog_json = "{_toml_escape(str(catalog_path))}"

[model_providers.{PROVIDER_NAME}]
name = "Codex Shim"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
experimental_bearer_token = "dummy"
request_max_retries = 3
stream_max_retries = 3
stream_idle_timeout_ms = 600000
'''
    path.write_text(text)
    return path


def codex_config_overrides(catalog_path: Path, default_slug: str, port: int) -> list[str]:
    return [
        f'model="{_toml_escape(default_slug)}"',
        f'model_provider="{PROVIDER_NAME}"',
        f'model_catalog_json="{_toml_escape(str(catalog_path))}"',
        f'model_providers.{PROVIDER_NAME}.name="Codex Shim"',
        f'model_providers.{PROVIDER_NAME}.base_url="http://127.0.0.1:{port}/v1"',
        f'model_providers.{PROVIDER_NAME}.wire_api="responses"',
        f'model_providers.{PROVIDER_NAME}.experimental_bearer_token="dummy"',
        f'model_providers.{PROVIDER_NAME}.request_max_retries=3',
        f'model_providers.{PROVIDER_NAME}.stream_max_retries=3',
        f'model_providers.{PROVIDER_NAME}.stream_idle_timeout_ms=600000',
    ]


def _default_context(model: ShimModel) -> int:
    lower = f"{model.model} {model.display_name}".lower()
    if "claude" in lower:
        return 200_000
    if "gpt-5" in lower:
        return 400_000
    if "gemini" in lower:
        return 1_000_000
    return 128_000


def _pi_context(entry: dict) -> int:
    value = entry.get("context_window") or entry.get("max_context_window")
    return int(value) if isinstance(value, (int, float)) else 272_000


def _pi_max_tokens(entry: dict, fallback: int) -> int:
    value = entry.get("max_output_tokens") or entry.get("max_tokens")
    return int(value) if isinstance(value, (int, float)) else fallback


def _pi_input(entry: dict) -> list[str]:
    value = entry.get("input_modalities")
    if not isinstance(value, list):
        return ["text"]
    result = [str(item) for item in value if item in {"text", "image"}]
    return result or ["text"]


def _reasoning_effort(model: ShimModel) -> str:
    lower = model.display_name.lower()
    if "xhigh" in lower or "x-high" in lower:
        return "xhigh"
    if "high" in lower:
        return "high"
    if "medium" in lower:
        return "medium"
    if "low" in lower:
        return "low"
    return "medium"


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
