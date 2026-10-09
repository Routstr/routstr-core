"""Price books for image models on catalogs that publish none per model.

Venice hands over a complete book per model. OpenAI, OpenRouter and Together
do not: OpenRouter's Image API itemises billable lines per endpoint, OpenAI
meters image output tokens and documents the per-image estimate in prose,
and Together lists prices only on its pricing page. Each builder here turns
one of those shapes into an ``ImagePricing`` so the rest of the proxy prices
every image model the same way.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx

from ..core.logging import get_logger
from ..payment.image_pricing import (
    RESOLUTION_MEGAPIXELS,
    ImagePriceTier,
    ImagePricing,
    produces_images,
)
from ..payment.models import Model

logger = get_logger(__name__)

__all__ = [
    "OPENROUTER_IMAGE_API_TIMEOUT_SECONDS",
    "attach_image_books",
    "fetch_openrouter_image_books",
    "openai_image_book",
    "openrouter_book_from_endpoints",
    "static_image_book",
]

OPENROUTER_IMAGE_API_TIMEOUT_SECONDS = 10.0
_OPENROUTER_IMAGE_API_CONCURRENCY = 8

# Megapixel-metered models are reserved at this class when the request and
# the catalog both leave the output size open.
_DEFAULT_MEGAPIXEL_CEILING_LABEL = "2K"

# USD per image at 1K, by quality, from OpenAI's image generation guide. The
# three documented sizes (1024x1024, 1024x1536, 1536x1024) all fall in the 1K
# class, so each entry is the dearest of them. These only size the
# reservation: the charge is settled on the tokens the response reports.
_OPENAI_IMAGE_USD_1K: dict[str, dict[str, float]] = {
    "gpt-image-2": {"low": 0.006, "medium": 0.053, "high": 0.211},
    "gpt-image-1.5": {"low": 0.013, "medium": 0.05, "high": 0.2},
    "gpt-image-1": {"low": 0.016, "medium": 0.063, "high": 0.25},
    "gpt-image-1-mini": {"low": 0.006, "medium": 0.015, "high": 0.052},
}
# GPT Image 2.5 adds ``xhigh`` and ``max`` above the GPT Image 2 steps; the
# per-image token counts are not documented, so they are reserved at
# multiples of ``high``.
_OPENAI_IMAGE_25_EXTRA_STEPS: dict[str, float] = {"xhigh": 2.0, "max": 4.0}
# OpenAI's default quality is ``auto``, "the best quality for the given
# model", so a request that names none is reserved like ``auto``: at the
# dearest step.
_OPENAI_IMAGE_DEFAULT_QUALITY = "auto"
# Ceiling on the tokens one reference image meters: OpenAI scales inputs to
# at most four 512px tiles (85 + 4 x 170) and adds at most 6,240 tokens for
# high input fidelity, so 8,192 covers every documented case.
_OPENAI_MAX_INPUT_IMAGE_TOKENS = 8_192
# Sizes above 1K are accepted but undocumented; the ceiling assumes the token
# count scales with area up to 4K.
_OPENAI_LARGER_SIZE_FACTOR = 4.0


def _float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _openai_family(model_id: str) -> str | None:
    bare = model_id.split("/")[-1].lower()
    if bare.startswith("gpt-image-2.5"):
        return "gpt-image-2.5"
    for family in sorted(_OPENAI_IMAGE_USD_1K, key=len, reverse=True):
        if bare == family or bare.startswith(family + "-"):
            return family
    return None


def openai_image_book(model_id: str, pricing: dict[str, Any]) -> ImagePricing | None:
    """A token book for an OpenAI GPT Image model.

    ``pricing`` is the OpenRouter catalog entry: ``image_output`` is USD per
    image output token, ``prompt`` per text input token and ``image_token``
    (or ``prompt`` again) per image input token. Tiers come from the
    documented per-image estimates and only size the reservation.
    """
    family = _openai_family(model_id)
    output_token_usd = _float(pricing.get("image_output"))
    if family is None or output_token_usd <= 0:
        return None

    steps = dict(
        _OPENAI_IMAGE_USD_1K["gpt-image-2" if family == "gpt-image-2.5" else family]
    )
    if family == "gpt-image-2.5":
        high = steps["high"]
        steps.update(
            {
                step: high * factor
                for step, factor in _OPENAI_IMAGE_25_EXTRA_STEPS.items()
            }
        )

    dearest_1k = max(steps.values())
    steps["auto"] = dearest_1k
    tiers = [
        ImagePriceTier(resolution="1K", quality=quality, usd=usd)
        for quality, usd in steps.items()
    ]
    input_text = _float(pricing.get("prompt"))
    input_image = _float(pricing.get("image_token")) or input_text

    return ImagePricing(
        max_usd=dearest_1k * _OPENAI_LARGER_SIZE_FACTOR,
        tiers=tiers,
        default_resolution="1K",
        default_quality=_OPENAI_IMAGE_DEFAULT_QUALITY,
        resolutions=["1K"],
        qualities=list(steps),
        unit="token",
        output_token_usd=output_token_usd,
        input_text_token_usd=input_text,
        input_image_token_usd=input_image,
        max_input_image_tokens=_OPENAI_MAX_INPUT_IMAGE_TOKENS,
    )


def _megapixel_tiers(
    megapixel_usd: float, resolutions: list[str]
) -> tuple[list[ImagePriceTier], float]:
    labels = [r.upper() for r in resolutions if r.upper() in RESOLUTION_MEGAPIXELS]
    if not labels:
        labels = [
            label
            for label in RESOLUTION_MEGAPIXELS
            if RESOLUTION_MEGAPIXELS[label]
            <= RESOLUTION_MEGAPIXELS[_DEFAULT_MEGAPIXEL_CEILING_LABEL]
        ]
    tiers = [
        ImagePriceTier(
            resolution=label, usd=megapixel_usd * RESOLUTION_MEGAPIXELS[label]
        )
        for label in labels
    ]
    return tiers, max(t.usd for t in tiers)


# OpenRouter lines that bound a request: a fixed price per output image, and
# a per-image surcharge for each reference image.
_REFERENCE_LINES = frozenset({"input_image", "input_reference"})
# Input lines that may be listed at zero without making the price unbounded.
_FREE_INPUT_LINES = frozenset(
    {"input_text", "input_font", "input_image", "input_reference"}
)


def _rate(value: Any) -> float | None:
    """A listed USD rate, or ``None`` if it is not a finite nonnegative number."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        rate = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(rate) or rate < 0:
        return None
    return rate


def _quotable_endpoint_book(endpoint: dict[str, Any]) -> ImagePricing | None:
    """The book of one endpoint, or ``None`` if its prices do not bound a request.

    Token and megapixel lines depend on quantities the node cannot cap before
    dispatch, and a billable line listed in two units is ambiguous. Variants
    are alternatives that no request field reliably names, so the dearest one
    is reserved.
    """
    lines = endpoint.get("pricing")
    if not isinstance(lines, list) or not lines:
        return None
    units: dict[str, str] = {}
    rates: dict[tuple[str, str], float] = {}
    for line in lines:
        if not isinstance(line, dict):
            return None
        billable, unit = line.get("billable"), line.get("unit")
        rate = _rate(line.get("cost_usd"))
        if not isinstance(billable, str) or not isinstance(unit, str) or rate is None:
            return None
        if units.setdefault(billable, unit) != unit:
            return None
        rates[(billable, unit)] = max(rates.get((billable, unit), 0.0), rate)

    output_usd = 0.0
    reference_usd = 0.0
    for (billable, unit), rate in rates.items():
        if billable == "output_image" and unit == "image":
            output_usd = rate
        elif billable in _REFERENCE_LINES and unit == "image":
            reference_usd = max(reference_usd, rate)
        elif rate > 0 or billable not in _FREE_INPUT_LINES:
            return None
    if output_usd <= 0:
        return None

    supported = endpoint.get("supported_parameters")
    return ImagePricing(
        max_usd=output_usd,
        tiers=[ImagePriceTier(usd=output_usd)],
        unit="image",
        input_image_usd=reference_usd,
        trust_upstream_cost=True,
        parameters={
            str(name): descriptor
            for name, descriptor in supported.items()
            if isinstance(descriptor, dict)
        }
        if isinstance(supported, dict)
        else {},
    )


def openrouter_book_from_endpoints(
    payload: dict[str, Any], model_id: str
) -> ImagePricing | None:
    """A book from ``GET /images/models/{id}/endpoints``, or ``None``.

    Requests are quoted on ``endpoints``: one book per endpoint whose prices
    bound a request, keyed by the ``provider_tag`` the request is pinned to.
    A tag pins a provider, not a record, so a tag listed twice leaves the
    price OpenRouter will charge ambiguous and neither record is quotable.
    A model with no quotable endpoint has no book and is not listed. The
    model-level book only carries the listed ceiling.
    """
    endpoints = payload.get("endpoints")
    if not isinstance(endpoints, list):
        return None
    tags = [e.get("provider_tag") for e in endpoints if isinstance(e, dict)]
    quotable: dict[str, ImagePricing] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        tag = endpoint.get("provider_tag")
        if not isinstance(tag, str) or not tag or tags.count(tag) != 1:
            continue
        book = _quotable_endpoint_book(endpoint)
        if book is not None:
            quotable[tag] = book.copy(update={"endpoint_tag": tag})
    if not quotable:
        logger.debug(
            "OpenRouter image model has no endpoint with bounded pricing",
            extra={"model_id": model_id},
        )
        return None
    max_usd = max(book.max_usd for book in quotable.values())
    return ImagePricing(
        max_usd=max_usd,
        tiers=[ImagePriceTier(usd=max_usd)],
        unit="image",
        input_image_usd=max(book.input_image_usd for book in quotable.values()),
        trust_upstream_cost=True,
        endpoints=quotable,
    )


async def fetch_openrouter_image_books(
    model_ids: list[str],
    *,
    base_url: str,
    api_key: str | None = None,
    timeout: float = OPENROUTER_IMAGE_API_TIMEOUT_SECONDS,
) -> dict[str, ImagePricing]:
    """Books for ``model_ids`` from OpenRouter's Image API, best effort.

    One request per model, bounded in parallel. A model whose endpoint
    listing fails or prices nothing is simply absent from the result.
    """
    books: dict[str, ImagePricing] = {}
    if not model_ids:
        return books

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    semaphore = asyncio.Semaphore(_OPENROUTER_IMAGE_API_CONCURRENCY)

    async def one(client: httpx.AsyncClient, model_id: str) -> None:
        url = f"{base_url.rstrip('/')}/images/models/{model_id}/endpoints"
        async with semaphore:
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
            except Exception as e:
                logger.debug(
                    "OpenRouter image endpoint listing unavailable",
                    extra={"model_id": model_id, "error": str(e)},
                )
                return
        book = (
            openrouter_book_from_endpoints(payload, model_id)
            if isinstance(payload, dict)
            else None
        )
        if book is not None:
            books[model_id] = book

    async with httpx.AsyncClient(timeout=timeout) as client:
        await asyncio.gather(*(one(client, model_id) for model_id in model_ids))
    return books


def static_image_book(
    usd: float,
    unit: str = "image",
    resolutions: list[str] | None = None,
    default_steps: int | None = None,
) -> ImagePricing | None:
    """A book from one published price, per image or per megapixel.

    For a per-megapixel price the tiers cover the nominal resolution classes
    so a request naming one is reserved at its area.
    """
    if usd <= 0:
        return None
    if unit == "megapixel":
        tiers, max_usd = _megapixel_tiers(usd, resolutions or [])
        return ImagePricing(
            max_usd=max_usd,
            tiers=tiers,
            default_resolution="1K",
            resolutions=[t.resolution for t in tiers if t.resolution],
            unit="megapixel",
            megapixel_usd=usd,
            default_steps=default_steps,
        )
    return ImagePricing(max_usd=usd, tiers=[ImagePriceTier(usd=usd)], unit="image")


def attach_image_books(
    models: list[Model], books: dict[str, ImagePricing], *, source: str
) -> list[Model]:
    """Carry each image model's book, and drop the image models without one.

    ``Pricing.image_output`` becomes the book's ceiling, which is what the
    fee and sats conversions map and what every tier is a ratio of, and
    ``Pricing.image`` the per-reference-image surcharge. An image model with
    no book would be served free, so it is not listed.
    """
    kept: list[Model] = []
    dropped: list[str] = []
    for model in models:
        if not produces_images(model):
            kept.append(model)
            continue
        book = books.get(model.id)
        if book is None or book.max_usd <= 0:
            dropped.append(model.id)
            continue
        pricing = model.pricing.copy(
            update={"image_output": book.max_usd, "image": book.input_image_usd}
        )
        kept.append(model.copy(update={"pricing": pricing, "image_pricing": book}))
    if dropped:
        logger.warning(
            f"({len(dropped)}) {source} image models skipped as unpriced",
            extra={"skipped_models": dropped},
        )
    return kept
