"""Recognising and reading image-generation responses.

These endpoints answer with images and, depending on the upstream, a usage
object that names tokens or a USD cost. What settlement needs from them is
here: is this an image endpoint, how many images came back, and what the
upstream said it metered. Pricing those images is ``payment.image_pricing``.

Nothing here imports the provider, so the provider can import this.
"""

from __future__ import annotations

import json
from typing import Any

from ..payment.image_pricing import ImageUsage

__all__ = [
    "ImageUsage",
    "is_image_generation_path",
    "parse_json_body",
    "read_image_response",
]

# OpenAI's images API plus the native image routes providers expose beside it.
_IMAGE_GENERATION_SUFFIXES = (
    "/images/generations",
    "/images/edits",
    "/images/variations",
    "/image/generate",
    "/image/edit",
    "/image/inpaint",
    "/image/upscale",
)


def is_image_generation_path(path: str) -> bool:
    return ("/" + path.strip("/")).endswith(_IMAGE_GENERATION_SUFFIXES)


def parse_json_body(request_body: bytes | None) -> dict:
    """The request body as a dict, or empty when absent or not JSON."""
    if not request_body:
        return {}
    try:
        parsed = json.loads(request_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _payload(content: bytes) -> dict | None:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _count(payload: dict) -> int:
    for field in ("data", "images"):
        value = payload.get(field)
        if isinstance(value, list):
            return len(value)
    return 0


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return 0


def _usd(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return 0.0
        return parsed if parsed > 0 else 0.0
    return 0.0


def read_image_response(content: bytes, is_json: bool) -> ImageUsage:
    """Images and metering an image response reports.

    Two usage dialects are read. OpenAI's images API counts
    ``input_tokens``/``output_tokens`` with ``input_tokens_details`` splitting
    input into ``text_tokens`` and ``image_tokens``; output tokens are all
    image tokens. OpenRouter's Image API uses ``prompt_tokens`` and
    ``completion_tokens`` and adds ``cost`` in USD, or ``cost_details.total_cost``
    on routers that itemise. A raw image body is one image.
    """
    if not is_json:
        # Raw image bytes (Venice /image/edit, /image/upscale).
        return ImageUsage(image_count=1 if content else 0)

    payload = _payload(content)
    if payload is None:
        return ImageUsage()

    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}

    input_tokens = _int(usage.get("input_tokens")) or _int(usage.get("prompt_tokens"))
    input_image_tokens = _int(details.get("image_tokens"))
    input_text_tokens = _int(details.get("text_tokens")) or max(
        input_tokens - input_image_tokens, 0
    )
    output_tokens = _int(usage.get("output_tokens")) or _int(
        usage.get("completion_tokens")
    )

    cost_details = usage.get("cost_details")
    cost_details = cost_details if isinstance(cost_details, dict) else {}
    upstream_cost = _usd(cost_details.get("total_cost")) or _usd(usage.get("cost"))

    return ImageUsage(
        image_count=_count(payload),
        input_text_tokens=input_text_tokens,
        input_image_tokens=input_image_tokens,
        output_image_tokens=output_tokens,
        upstream_cost_usd=upstream_cost,
    )
