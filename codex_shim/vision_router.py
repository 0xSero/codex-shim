"""
Smart Vision Routing for codex-shim

Automatically routes requests with images to vision-capable models and strips
images from history when routing to text-only models.

This module provides intelligent model selection based on visual content detection,
allowing seamless switching between vision and text models within the same conversation.
"""

from __future__ import annotations

import os
from typing import Any

from .settings import ShimModel


def is_vision_routing_enabled() -> bool:
    """Check if smart vision routing is enabled via environment variable."""
    return os.environ.get("CODEX_SHIM_VISION_ROUTING", "").lower() in {"1", "true", "yes", "on"}


def has_image_in_current_turn(input_data: Any) -> bool:
    """
    Detect if the CURRENT (latest) user turn contains image content.

    This is different from has_image_in_input which checks the entire history.
    We only check items that belong to the current turn — everything since the
    latest assistant response — to determine if the current request requires
    vision capabilities.

    Args:
        input_data: The input field from a Responses API request

    Returns:
        True if the current turn has image content
    """
    if not isinstance(input_data, list):
        return False

    # Walk backwards. The previous assistant message marks the end of the
    # prior turn; anything after it (i.e. items we visit before hitting an
    # assistant) is part of the current user turn.
    for item in reversed(input_data):
        if not isinstance(item, dict):
            continue

        if item.get("role") == "assistant":
            break

        if item.get("type") == "input_image":
            return True

        if _has_image_in_content(item.get("content")):
            return True

    return False


def has_image_in_input(input_data: Any) -> bool:
    """
    Detect if the input contains any image content (checks entire history).

    Checks for:
    - input_image type items
    - image_url in content
    - computer_call_output with screenshots
    - function_call_output with visual content

    Args:
        input_data: The input field from a Responses API request

    Returns:
        True if any image content is detected
    """
    if input_data is None:
        return False

    if isinstance(input_data, str):
        return False

    if not isinstance(input_data, list):
        return False

    for item in input_data:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")

        # Direct image types
        if item_type == "input_image":
            return True

        # Computer use screenshots
        if item_type == "computer_call_output":
            output = item.get("output", {})
            if isinstance(output, dict) and output.get("type") == "input_image":
                return True

        # Function call outputs with images
        if item_type == "function_call_output":
            output = item.get("output")
            if _has_image_in_content(output):
                return True

        # Message content with images
        content = item.get("content")
        if _has_image_in_content(content):
            return True

    return False


def _has_image_in_content(content: Any) -> bool:
    """Check if content contains image data."""
    if content is None:
        return False

    if isinstance(content, str):
        return False

    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                part_type = part.get("type")
                if part_type in {"input_image", "image_url"}:
                    return True
                if "image_url" in part:
                    return True
        return False

    if isinstance(content, dict):
        content_type = content.get("type")
        if content_type in {"input_image", "image_url"}:
            return True
        if "image_url" in content:
            return True

    return False


def has_image_in_recent_turns(input_data: Any, recent_turns: int = 3) -> bool:
    """
    Check if images appear in the most recent N conversation turns.

    This is useful for determining if the conversation context still requires
    a vision model, even if the latest user message is text-only.

    Args:
        input_data: The input field from a Responses API request
        recent_turns: Number of recent turns to check (default: 3)

    Returns:
        True if images are found in recent turns
    """
    if not isinstance(input_data, list):
        return False

    # A "turn" is one round of dialogue: a user message plus any following
    # assistant/tool messages. We count user messages walking from the end —
    # each user message marks the start of a new turn.
    turn_count = 0
    for item in reversed(input_data):
        if not isinstance(item, dict):
            continue

        if item.get("role") == "user":
            turn_count += 1
            if turn_count > recent_turns:
                break

        # Check for images in this item
        if has_image_in_input([item]):
            return True

    return False


def strip_images_from_history(body: dict[str, Any]) -> dict[str, Any]:
    """
    Remove image content from request history, replacing with text descriptions.

    This allows text-only models to process conversations that previously
    contained images by preserving the assistant's descriptions while removing
    the actual image data.

    Args:
        body: The Responses API request body

    Returns:
        Modified request body with images replaced by text placeholders
    """
    body = dict(body)  # Shallow copy
    input_items = body.get("input")

    if not isinstance(input_items, list):
        return body

    cleaned_items = []

    for item in input_items:
        if not isinstance(item, dict):
            cleaned_items.append(item)
            continue

        item = dict(item)  # Copy the item
        item_type = item.get("type")

        # Replace top-level input_image items
        if item_type == "input_image":
            cleaned_items.append({
                "type": "input_text",
                "text": "[Image from previous turn - see assistant's description above for visual details]"
            })
            continue

        # Process content field
        content = item.get("content")
        if content is not None:
            item["content"] = _strip_images_from_content(content)

        # Process computer_call_output
        if item_type == "computer_call_output":
            output = item.get("output")
            if isinstance(output, dict) and output.get("type") == "input_image":
                item["output"] = {
                    "type": "input_text",
                    "text": "[Screenshot from computer use - visual content not available to this model]"
                }

        # Process function_call_output
        if item_type == "function_call_output":
            output = item.get("output")
            if _has_image_in_content(output):
                item["output"] = _strip_images_from_content(output)

        cleaned_items.append(item)

    body["input"] = cleaned_items
    return body


def _strip_images_from_content(content: Any) -> Any:
    """Strip images from content, replacing with text descriptions."""
    if content is None:
        return content

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        cleaned = []
        for part in content:
            if isinstance(part, dict):
                part_type = part.get("type")
                if part_type in {"input_image", "image_url"} or "image_url" in part:
                    # Replace image with text description
                    cleaned.append({
                        "type": "input_text",
                        "text": "[Image from previous turn - see assistant's description above for visual details]"
                    })
                else:
                    cleaned.append(part)
            else:
                cleaned.append(part)
        return cleaned

    if isinstance(content, dict):
        content_type = content.get("type")
        if content_type in {"input_image", "image_url"} or "image_url" in content:
            return {
                "type": "input_text",
                "text": "[Image from previous turn - see assistant's description above for visual details]"
            }
        return content

    return content


def select_model_with_vision_routing(
    body: dict[str, Any],
    models: list[ShimModel],
    default_slug: str | None = None
) -> tuple[ShimModel, dict[str, Any]]:
    """
    Intelligently select a model based on visual content in the request.

    Strategy:
    1. If current turn has images -> use vision-capable model
    2. If no images in current turn:
       a. Use default model (or first model if no default specified)
       b. If default model doesn't support vision, strip images from history

    Args:
        body: The Responses API request body
        models: List of available models
        default_slug: Optional default model slug to prefer for text-only requests

    Returns:
        Tuple of (selected_model, potentially_modified_body)

    Raises:
        ValueError: If no suitable model is found
    """
    if not models:
        raise ValueError("No models available for routing")

    # Check if CURRENT turn contains images (not entire history)
    has_current_image = has_image_in_current_turn(body.get("input"))

    if has_current_image:
        # Need a vision-capable model
        vision_model = _get_vision_capable_model(models)
        if vision_model is None:
            raise ValueError(
                "Request contains images but no vision-capable models are configured. "
                "Add a model without 'no_image_support: true' to handle visual content."
            )
        return vision_model, body

    # No images in current turn - use default model
    default_model = _get_default_model(models, default_slug)

    # If default model doesn't support vision, clean up history
    if default_model.no_image_support:
        body = strip_images_from_history(body)

    return default_model, body


def _get_vision_capable_model(models: list[ShimModel]) -> ShimModel | None:
    """Find the first model that supports vision."""
    for model in models:
        if not model.no_image_support:
            return model
    return None


def _get_default_model(models: list[ShimModel], preferred_slug: str | None = None) -> ShimModel:
    """
    Get the default model for text-only requests.

    Priority:
    1. Model matching preferred_slug
    2. First model in the list
    """
    if preferred_slug:
        for model in models:
            if model.slug == preferred_slug:
                return model

    return models[0]


def get_routing_config() -> dict[str, Any]:
    """
    Get current vision routing configuration from environment variables.

    Returns:
        Dictionary with routing configuration
    """
    return {
        "enabled": is_vision_routing_enabled(),
        "recent_turns_threshold": int(os.environ.get("CODEX_SHIM_VISION_RECENT_TURNS", "3")),
        "default_text_model": os.environ.get("CODEX_SHIM_DEFAULT_TEXT_MODEL", ""),
    }
