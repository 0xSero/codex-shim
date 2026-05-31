"""
Tests for smart vision routing functionality
"""

from codex_shim.vision_router import (
    has_image_in_input,
    has_image_in_recent_turns,
    strip_images_from_history,
    select_model_with_vision_routing,
)
from codex_shim.settings import ShimModel


def test_detect_input_image():
    """Test detection of input_image type items"""
    body = {
        "input": [
            {"type": "input_text", "text": "What is this?"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
        ]
    }
    assert has_image_in_input(body["input"]) is True


def test_detect_image_in_message_content():
    """Test detection of images in message content"""
    body = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe this"},
                    {"type": "input_image", "image_url": "data:image/png;base64,BBB"},
                ],
            }
        ]
    }
    assert has_image_in_input(body["input"]) is True


def test_detect_computer_call_output_screenshot():
    """Test detection of screenshots in computer_call_output"""
    body = {
        "input": [
            {
                "type": "computer_call_output",
                "call_id": "cu_1",
                "output": {"type": "input_image", "image_url": "data:image/png;base64,CCC"},
            }
        ]
    }
    assert has_image_in_input(body["input"]) is True


def test_detect_function_call_output_with_image():
    """Test detection of images in function_call_output"""
    body = {
        "input": [
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [{"type": "input_image", "image_url": "data:image/png;base64,DDD"}],
            }
        ]
    }
    assert has_image_in_input(body["input"]) is True


def test_no_image_in_text_only_input():
    """Test that text-only input returns False"""
    body = {
        "input": [
            {"type": "input_text", "text": "Hello"},
            {"role": "user", "content": [{"type": "input_text", "text": "World"}]},
        ]
    }
    assert has_image_in_input(body["input"]) is False


def test_strip_images_from_user_content():
    """Test stripping images from user message content"""
    body = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What is this?"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
                ],
            }
        ]
    }

    cleaned = strip_images_from_history(body)
    content = cleaned["input"][0]["content"]

    assert len(content) == 2
    assert content[0]["type"] == "input_text"
    assert content[0]["text"] == "What is this?"
    assert content[1]["type"] == "input_text"
    assert "Image from previous turn" in content[1]["text"]


def test_strip_images_preserves_assistant_responses():
    """Test that assistant responses are preserved when stripping images"""
    body = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What is this?"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "This is a cat"}],
            },
            {"role": "user", "content": [{"type": "input_text", "text": "What breed?"}]},
        ]
    }

    cleaned = strip_images_from_history(body)

    # Assistant response should be unchanged
    assert cleaned["input"][1]["role"] == "assistant"
    assert cleaned["input"][1]["content"][0]["text"] == "This is a cat"


def test_strip_computer_call_output_screenshot():
    """Test stripping screenshots from computer_call_output"""
    body = {
        "input": [
            {
                "type": "computer_call_output",
                "call_id": "cu_1",
                "output": {"type": "input_image", "image_url": "data:image/png;base64,BBB"},
            }
        ]
    }

    cleaned = strip_images_from_history(body)
    output = cleaned["input"][0]["output"]

    assert output["type"] == "input_text"
    assert "Screenshot from computer use" in output["text"]


def test_has_image_in_recent_turns():
    """Test detection of images in recent conversation turns"""
    body = {
        "input": [
            # Old turn with image (turn 1)
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Old question"},
                    {"type": "input_image", "image_url": "data:image/png;base64,OLD"},
                ],
            },
            {"role": "assistant", "content": [{"type": "output_text", "text": "Old answer"}]},
            # Turn 2
            {"role": "user", "content": [{"type": "input_text", "text": "Question 2"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "Answer 2"}]},
            # Turn 3
            {"role": "user", "content": [{"type": "input_text", "text": "Question 3"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "Answer 3"}]},
            # Turn 4 (most recent)
            {"role": "user", "content": [{"type": "input_text", "text": "Latest question"}]},
        ]
    }

    # Image is in turn 1, checking recent 3 turns should not find it
    assert has_image_in_recent_turns(body["input"], recent_turns=3) is False

    # Checking recent 4 turns should find it
    assert has_image_in_recent_turns(body["input"], recent_turns=4) is True


def test_select_model_with_vision_routing_image_request():
    """Test model selection when request contains images"""
    models = [
        ShimModel(
            slug="deepseek-chat",
            model="deepseek-chat",
            display_name="DeepSeek",
            provider="openai",
            base_url="https://api.deepseek.com",
            no_image_support=True,
        ),
        ShimModel(
            slug="kimi-k1",
            model="moonshot-v1-128k",
            display_name="Kimi",
            provider="openai",
            base_url="https://api.moonshot.cn/v1",
            no_image_support=False,
        ),
    ]

    body = {
        "model": "deepseek-chat",
        "input": [
            {"type": "input_text", "text": "What is this?"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
        ],
    }

    selected_model, modified_body = select_model_with_vision_routing(body, models, default_slug="deepseek-chat")

    # Should select vision-capable model (Kimi) instead of requested DeepSeek
    assert selected_model.slug == "kimi-k1"
    assert selected_model.no_image_support is False

    # Body should be unchanged (image preserved)
    assert has_image_in_input(modified_body["input"]) is True


def test_select_model_with_vision_routing_text_only_request():
    """Test model selection when request is text-only"""
    models = [
        ShimModel(
            slug="deepseek-chat",
            model="deepseek-chat",
            display_name="DeepSeek",
            provider="openai",
            base_url="https://api.deepseek.com",
            no_image_support=True,
        ),
        ShimModel(
            slug="kimi-k1",
            model="moonshot-v1-128k",
            display_name="Kimi",
            provider="openai",
            base_url="https://api.moonshot.cn/v1",
            no_image_support=False,
        ),
    ]

    body = {
        "model": "deepseek-chat",
        "input": [
            {"type": "input_text", "text": "Previous question with image"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "This is a cat"}]},
            {"type": "input_text", "text": "What breed is it?"},  # Current text-only question
        ],
    }

    selected_model, modified_body = select_model_with_vision_routing(body, models, default_slug="deepseek-chat")

    # Should use default model (DeepSeek)
    assert selected_model.slug == "deepseek-chat"

    # Body should have images stripped from history
    assert has_image_in_input(modified_body["input"]) is False

    # Assistant response should be preserved
    assistant_msg = [item for item in modified_body["input"] if isinstance(item, dict) and item.get("role") == "assistant"][0]
    assert assistant_msg["content"][0]["text"] == "This is a cat"


def test_select_model_no_vision_capable_model_available():
    """Test error when images are present but no vision model is configured"""
    models = [
        ShimModel(
            slug="deepseek-chat",
            model="deepseek-chat",
            display_name="DeepSeek",
            provider="openai",
            base_url="https://api.deepseek.com",
            no_image_support=True,
        ),
    ]

    body = {
        "model": "deepseek-chat",
        "input": [
            {"type": "input_text", "text": "What is this?"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
        ],
    }

    try:
        select_model_with_vision_routing(body, models)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "no vision-capable models" in str(e).lower()
