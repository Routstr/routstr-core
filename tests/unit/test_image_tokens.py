import base64
from io import BytesIO

import pytest
from PIL import Image

from routstr.payment.cost import (
    _calculate_image_tokens,
    _estimate_image_tokens_in_messages,
    _get_image_dimensions,
)


def create_test_image(width: int, height: int) -> bytes:
    """Create a test image with specified dimensions."""
    img = Image.new("RGB", (width, height), color="red")
    buffer = BytesIO()
    img.save(buffer, format="JPEG")
    return buffer.getvalue()


def test_calculate_image_tokens_low_detail() -> None:
    """Test that low detail images always return 85 tokens."""
    assert _calculate_image_tokens(100, 100, "low") == 85
    assert _calculate_image_tokens(1000, 1000, "low") == 85
    assert _calculate_image_tokens(2048, 2048, "low") == 85


def test_calculate_image_tokens_high_detail_small() -> None:
    """Test token calculation for small images."""
    tokens = _calculate_image_tokens(512, 512, "high")
    assert tokens == 85 + 170


def test_calculate_image_tokens_high_detail_large() -> None:
    """Test token calculation for large images that need tiling."""
    tokens = _calculate_image_tokens(768, 768, "high")
    assert tokens > 85


def test_calculate_image_tokens_auto() -> None:
    """Test that auto detail behaves like high detail."""
    width, height = 512, 512
    auto_tokens = _calculate_image_tokens(width, height, "auto")
    high_tokens = _calculate_image_tokens(width, height, "high")
    assert auto_tokens == high_tokens


def test_get_image_dimensions() -> None:
    """Test extracting dimensions from image bytes."""
    image_bytes = create_test_image(800, 600)
    width, height = _get_image_dimensions(image_bytes)
    assert width == 800
    assert height == 600


def test_get_image_dimensions_invalid() -> None:
    """Test that invalid image data returns default dimensions."""
    invalid_bytes = b"not an image"
    width, height = _get_image_dimensions(invalid_bytes)
    assert width == 512
    assert height == 512


@pytest.mark.asyncio
async def test_estimate_image_tokens_base64() -> None:
    """Test estimating tokens for base64 encoded images."""
    image_bytes = create_test_image(512, 512)
    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                },
            ],
        }
    ]

    tokens = await _estimate_image_tokens_in_messages(messages)
    assert tokens > 0


@pytest.mark.asyncio
async def test_estimate_image_tokens_multiple_images() -> None:
    """Test estimating tokens for multiple images."""
    image_bytes = create_test_image(512, 512)
    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Compare these images"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                },
            ],
        }
    ]

    tokens = await _estimate_image_tokens_in_messages(messages)
    assert tokens > 0


@pytest.mark.asyncio
async def test_estimate_image_tokens_with_detail() -> None:
    """Test that detail parameter affects token calculation."""
    image_bytes = create_test_image(512, 512)
    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    messages_low = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}",
                        "detail": "low",
                    },
                },
            ],
        }
    ]

    messages_high = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]

    tokens_low = await _estimate_image_tokens_in_messages(messages_low)
    tokens_high = await _estimate_image_tokens_in_messages(messages_high)

    assert tokens_low == 85
    assert tokens_high > tokens_low


@pytest.mark.asyncio
async def test_estimate_image_tokens_no_images() -> None:
    """Test that messages without images return 0 tokens."""
    messages = [
        {"role": "user", "content": "Hello, how are you?"},
        {"role": "assistant", "content": "I'm doing well, thank you!"},
    ]

    tokens = await _estimate_image_tokens_in_messages(messages)
    assert tokens == 0


@pytest.mark.asyncio
async def test_estimate_image_tokens_input_image_type() -> None:
    """Test that input_image type is also supported."""
    image_bytes = create_test_image(512, 512)
    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{base64_image}",
                },
            ],
        }
    ]

    tokens = await _estimate_image_tokens_in_messages(messages)
    assert tokens > 0
