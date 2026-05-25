from __future__ import annotations

import json
from pathlib import Path

from .settings import CHATGPT_MODEL_SLUG, PROVIDER_NAME, ShimModel, chatgpt_passthrough_available, default_model_slug


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


def chatgpt_passthrough_entry() -> dict:
    """Catalog entry for the original GPT-5.5 routed through ChatGPT passthrough."""
    return {
        "slug": CHATGPT_MODEL_SLUG,
        "display_name": "GPT-5.5",
        "description": "OpenAI GPT-5.5 — the default Codex model, routed through ChatGPT passthrough.",
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
        "isDefault": True,
        "priority": 10000,
        "prefer_websockets": False,
        "available_in_plans": PLAN_TIERS,
        "base_instructions": "You are Codex, a coding agent powered by GPT-5.5.",
        "model_messages": {
            "instructions_template": "You are Codex, a coding agent powered by GPT-5.5.",
            "instructions_variables": {"model_name": "GPT-5.5"},
        },
    }


def write_catalog(models: list[ShimModel], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    if chatgpt_passthrough_available():
        entries.append(chatgpt_passthrough_entry())
    entries.extend(catalog_entry(model) for model in models)
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

