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
import re
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
    "openrouter_book_from_pricing",
    "static_image_book",
]

OPENROUTER_IMAGE_API_TIMEOUT_SECONDS = 10.0
_OPENROUTER_IMAGE_API_CONCURRENCY = 8

# A token-metered model whose per-image token count is undocumented is
# reserved at this many output tokens, past the dearest documented tier.
_ESTIMATED_MAX_OUTPUT_IMAGE_TOKENS = 8_192

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
_OPENAI_IMAGE_DEFAULT_QUALITY = "medium"
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

    tiers = [
        ImagePriceTier(resolution="1K", quality=quality, usd=usd)
        for quality, usd in steps.items()
    ]
    dearest_1k = max(steps.values())
    input_text = _float(pricing.get("prompt"))
    input_image = _float(pricing.get("image_token")) or input_text

    return ImagePricing(
        max_usd=dearest_1k * _OPENAI_LARGER_SIZE_FACTOR,
        tiers=tiers,
        default_resolution="1K",
        default_quality=_OPENAI_IMAGE_DEFAULT_QUALITY,
        resolutions=["1K"],
        qualities=[*steps.keys(), "auto"],
        unit="token",
        output_token_usd=output_token_usd,
        input_text_token_usd=input_text,
        input_image_token_usd=input_image,
    )


_RESOLUTION_TOKEN = re.compile(r"^\d+k$", re.IGNORECASE)


def _split_variant(variant: Any) -> tuple[str | None, str | None]:
    """An OpenRouter pricing ``variant`` as ``(resolution, quality)``.

    Variants are ``2k``, ``low_1k``, ``medium_2k`` or a bare word such as
    ``high_resolution``. Tokens shaped like a resolution class name one; the
    rest, joined back, is the quality step.
    """
    if not isinstance(variant, str) or not variant:
        return None, None
    tokens = variant.split("_")
    resolution = next((t.upper() for t in tokens if _RESOLUTION_TOKEN.match(t)), None)
    rest = [t for t in tokens if not _RESOLUTION_TOKEN.match(t)]
    quality = "_".join(rest).lower() if rest else None
    return resolution, quality


def _enum_values(supported: Any, name: str) -> list[str]:
    if not isinstance(supported, dict):
        return []
    descriptor = supported.get(name)
    if not isinstance(descriptor, dict):
        return []
    values = descriptor.get("values")
    if not isinstance(values, list):
        return []
    return [str(v) for v in values]


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


def openrouter_book_from_endpoints(
    payload: dict[str, Any], model_id: str
) -> ImagePricing | None:
    """A book from ``GET /images/models/{id}/endpoints``.

    Each endpoint lists billable lines ``{billable, unit, cost_usd, variant}``.
    The dearest line per tier across endpoints is kept, since the router
    picks the endpoint. A flat per-image line wins the unit over a megapixel
    line, which wins over a token line; whichever it is, settlement trusts
    the USD the response reports. An ``input_image`` line per image is the
    reference-image surcharge.
    """
    endpoints = payload.get("endpoints")
    if not isinstance(endpoints, list):
        return None

    per_image: dict[tuple[str | None, str | None], float] = {}
    megapixel_usd = 0.0
    output_token_usd = 0.0
    input_text_usd = 0.0
    input_image_usd = 0.0
    reference_usd = 0.0
    resolutions: list[str] = []
    qualities: list[str] = []

    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        supported = endpoint.get("supported_parameters")
        for value in _enum_values(supported, "resolution"):
            if value.upper() not in resolutions:
                resolutions.append(value.upper())
        for value in _enum_values(supported, "quality"):
            if value.lower() not in qualities:
                qualities.append(value.lower())
        lines = endpoint.get("pricing")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if not isinstance(line, dict):
                continue
            billable = line.get("billable")
            unit = line.get("unit")
            usd = _float(line.get("cost_usd"))
            if usd <= 0:
                continue
            if billable == "output_image":
                if unit == "image":
                    key = _split_variant(line.get("variant"))
                    per_image[key] = max(per_image.get(key, 0.0), usd)
                elif unit == "megapixel":
                    megapixel_usd = max(megapixel_usd, usd)
                elif unit == "token":
                    output_token_usd = max(output_token_usd, usd)
            elif billable == "input_text" and unit == "token":
                input_text_usd = max(input_text_usd, usd)
            elif billable == "input_image" and unit == "token":
                input_image_usd = max(input_image_usd, usd)
            elif billable == "input_image" and unit == "image":
                reference_usd = max(reference_usd, usd)

    if per_image:
        tiers = [
            ImagePriceTier(resolution=resolution, quality=quality, usd=usd)
            for (resolution, quality), usd in per_image.items()
        ]
        untiered = per_image.get((None, None))
        cheapest = _cheapest(per_image)
        return ImagePricing(
            max_usd=max(per_image.values()),
            tiers=tiers,
            # A single untiered price applies whatever the request asks for.
            default_resolution=None if untiered is not None else cheapest[0],
            default_quality=None if untiered is not None else cheapest[1],
            resolutions=resolutions if untiered is None else [],
            qualities=qualities if untiered is None else [],
            unit="image",
            input_image_usd=reference_usd,
            trust_upstream_cost=True,
        )

    if megapixel_usd > 0:
        tiers, max_usd = _megapixel_tiers(megapixel_usd, resolutions)
        return ImagePricing(
            max_usd=max_usd,
            tiers=tiers,
            default_resolution="1K",
            resolutions=[t.resolution for t in tiers if t.resolution],
            qualities=qualities,
            unit="megapixel",
            megapixel_usd=megapixel_usd,
            input_image_usd=reference_usd,
            trust_upstream_cost=True,
        )

    if output_token_usd > 0:
        book = openai_image_book(
            model_id,
            {
                "image_output": output_token_usd,
                "prompt": input_text_usd,
                "image_token": input_image_usd,
            },
        )
        if book is None:
            estimate = output_token_usd * _ESTIMATED_MAX_OUTPUT_IMAGE_TOKENS
            book = ImagePricing(
                max_usd=estimate,
                tiers=[ImagePriceTier(usd=estimate)],
                qualities=qualities,
                unit="token",
                output_token_usd=output_token_usd,
                input_text_token_usd=input_text_usd,
                input_image_token_usd=input_image_usd,
            )
        return book.copy(
            update={"trust_upstream_cost": True, "input_image_usd": reference_usd}
        )

    return None


def _cheapest(
    per_image: dict[tuple[str | None, str | None], float],
) -> tuple[str | None, str | None]:
    """The tier a request naming nothing is priced at: the cheapest one."""
    labelled = {k: v for k, v in per_image.items() if k != (None, None)}
    if not labelled:
        return None, None
    return min(labelled, key=lambda k: labelled[k])


def openrouter_book_from_pricing(
    pricing: dict[str, Any], model_id: str
) -> ImagePricing | None:
    """A book from the ``/models`` pricing keys, for when the Image API is down.

    ``image_output`` is USD per image output token on every image model the
    catalog lists; ``prompt`` is the text input rate. The flat ``image`` key
    there is a per-input-image surcharge, not a generation price, so it is
    not read as one.
    """
    output_token_usd = _float(pricing.get("image_output"))
    if output_token_usd <= 0:
        return None
    book = openrouter_book_from_endpoints(
        {
            "endpoints": [
                {
                    "pricing": [
                        {
                            "billable": "output_image",
                            "unit": "token",
                            "cost_usd": output_token_usd,
                        },
                        {
                            "billable": "input_text",
                            "unit": "token",
                            "cost_usd": _float(pricing.get("prompt")),
                        },
                        {
                            "billable": "input_image",
                            "unit": "token",
                            "cost_usd": _float(pricing.get("image_token")),
                        },
                    ]
                }
            ]
        },
        model_id,
    )
    return book


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
