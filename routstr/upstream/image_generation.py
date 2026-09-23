"""Recognising and reading image-generation responses.

These endpoints answer with images and no usage object, so the settlement path
that reads tokens has nothing to work from. What it needs instead is here: is
this an image endpoint, and how many images came back. Pricing those images is
``payment.image_pricing``.

Nothing here imports the provider, so the provider can import this.
"""

from __future__ import annotations

import json

__all__ = [
    "count_generated_images",
    "is_image_generation_path",
    "parse_json_body",
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


def count_generated_images(content: bytes, is_json: bool) -> int:
    """How many images the response carried.

    A response that produced nothing counts zero and is never billed; the
    reservation is released instead.
    """
    if not is_json:
        # Raw image bytes (Venice /image/edit, /image/upscale).
        return 1 if content else 0
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    for field in ("data", "images"):
        value = payload.get(field)
        if isinstance(value, list):
            return len(value)
    return 0
