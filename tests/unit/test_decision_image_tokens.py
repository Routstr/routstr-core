"""System One image token estimates.

Expected values are OpenAI's published examples, so a litellm regression fails here.
"""

from __future__ import annotations

import base64
import json
import time
from io import BytesIO
from typing import Any
from unittest.mock import patch

import pytest
from PIL import Image

from routstr.upstream.base import _apply_estimated_usage
from routstr.upstream.count_tokens import (
    MissingUsageEstimator,
    decision_image_tokens,
)

DEFAULT_IMAGE_TOKENS = 250  # litellm.constants.DEFAULT_IMAGE_TOKEN_COUNT


def _b64_image(width: int, height: int, fmt: str = "JPEG") -> str:
    buf = BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode()


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


_DECISION = {
    "model": "clef:27b",
    "state": "Classify the attached image.",
    "questions": {"subject": {"type": "noul", "instructions": "Is it a cat?"}},
}


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (500, 263, 255),  # one tile
        (512, 512, 255),  # exactly one tile
        (513, 512, 425),  # spills into a second tile
        (1024, 1024, 765),  # OpenAI docs: 1024x1024 high -> 765
        (2048, 4096, 1105),  # OpenAI docs: 2048x4096 high -> 1105
        (16, 16, 255),  # tiny images still cost one tile
    ],
)
def test_tile_count_matches_openai_examples(
    width: int, height: int, expected: int
) -> None:
    assert decision_image_tokens(_b64_image(width, height)) == expected


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "GIF"])
def test_every_supported_format_is_measured(fmt: str) -> None:
    assert decision_image_tokens(_b64_image(1024, 1024, fmt)) == 765


def test_raw_base64_and_data_url_agree() -> None:
    raw = _b64_image(1024, 1024)
    assert decision_image_tokens(raw) == decision_image_tokens(
        f"data:image/jpeg;base64,{raw}"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://example.com/cat.jpg",
        "HTTPS://EXAMPLE.COM/cat.jpg",
        "  http://example.com/cat.jpg",
    ],
)
def test_urls_are_never_fetched(url: str) -> None:
    """litellm fetches http(s) image URLs; the node must not (SSRF)."""
    with patch(
        "litellm.litellm_core_utils.token_counter.safe_get",
        side_effect=AssertionError("image URL was fetched"),
    ):
        assert decision_image_tokens(url) == DEFAULT_IMAGE_TOKENS


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xd8" + b"\xff\xe0\x00\x00" + b"\x00" * 64,  # zero segment length
        b"\xff\xd8" + b"\xff\xe0\x00\x02" * 8,  # minimal segments to EOF
        b"\xff\xd8\xff\xe0\x00\x10JFIF",  # truncated JPEG
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 4,  # truncated PNG header
        b"not an image at all" * 10,
    ],
)
def test_malformed_images_fall_back_to_default(payload: bytes) -> None:
    raw = base64.b64encode(payload).decode()
    tokens = decision_image_tokens(raw)
    assert tokens in (DEFAULT_IMAGE_TOKENS, 255)  # default, or default dims


@pytest.mark.parametrize("value", ["", "!!!not base64!!!", None, 42, {"url": "x"}, []])
def test_unusable_entries_cost_the_default(value: object) -> None:
    assert decision_image_tokens(value) == DEFAULT_IMAGE_TOKENS


def test_large_payload_is_bounded() -> None:
    raw = base64.b64encode(b"\xff\xd8" + b"\x00" * 5_000_000).decode()
    started = time.perf_counter()
    decision_image_tokens(raw)
    assert time.perf_counter() - started < 2.0


def _estimated_input(payload: dict[str, Any]) -> int:
    usage = MissingUsageEstimator(_body(payload), None).response_data()["usage"]
    return int(usage["input_tokens"])


def test_estimator_adds_image_tokens_to_text_tokens() -> None:
    text_only = _estimated_input(_DECISION)
    images = [_b64_image(500, 263), _b64_image(1024, 1024), "https://x/y.jpg"]

    assert (
        _estimated_input({**_DECISION, "images": images}) == text_only + 255 + 765 + 250
    )


def test_estimator_counts_many_images() -> None:
    text_only = _estimated_input(_DECISION)
    images = [_b64_image(500, 263)] * 10

    assert _estimated_input({**_DECISION, "images": images}) == text_only + 10 * 255


def test_estimator_accepts_a_single_image_string() -> None:
    text_only = _estimated_input(_DECISION)

    assert (
        _estimated_input({**_DECISION, "images": _b64_image(500, 263)})
        == text_only + 255
    )


@pytest.mark.parametrize("images", [[], None, {"a": 1}])
def test_estimator_without_usable_images_is_text_only(images: object) -> None:
    assert _estimated_input({**_DECISION, "images": images}) == _estimated_input(
        _DECISION
    )


def test_base64_never_inflates_the_text_estimate() -> None:
    huge = base64.b64encode(b"\x00" * 81_000).decode()  # ~108k chars, undecodable image

    assert (
        _estimated_input({**_DECISION, "images": [huge]})
        < _estimated_input(_DECISION) + 300
    )


def test_x_cashu_settlement_bills_image_estimate() -> None:
    response: dict[str, Any] = {
        "model": "clef:27b",
        "answers": {"subject": {"type": "noul", "noul": 0.9}},
    }
    body = {**_DECISION, "images": [_b64_image(1024, 1024)]}

    _apply_estimated_usage(response, _body(body), None, 1000, "sat", "chat")

    assert response["usage"]["input_tokens"] == _estimated_input(_DECISION) + 765
    assert response["usage"]["output_tokens"] == 0
