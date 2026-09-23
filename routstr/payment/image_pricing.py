"""Per-image pricing for models that return images instead of tokens.

The same model costs a different amount per resolution, and the newer ones per
quality step within a resolution, so the price is only known once the request
is read.

``Pricing.image`` carries only the ceiling so it stays a flat numeric record
the provider-fee and sats conversions can map over. Tier prices here are raw
upstream USD and are applied as a ratio against that ceiling, which is why
neither conversion is repeated below.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pydantic.v1 import BaseModel

if TYPE_CHECKING:
    from .models import Model

__all__ = [
    "MAX_RESERVED_IMAGES",
    "ImagePriceTier",
    "ImagePricing",
    "image_reservation_msats",
    "per_image_sats",
    "produces_images",
    "select_image_price_usd",
]

# 1K is nominally 1024x1024, 2K is 2048x2048. A pixel size maps onto a class by
# area, bounded at twice the nominal area so a class's portrait and landscape
# variants (1024x1536) stay in it. A size exactly on a boundary takes the
# dearer class rather than under-billing.
_RESOLUTION_AREAS: tuple[tuple[int, str], ...] = (
    (2 * 1024 * 1024, "1K"),
    (2 * 2048 * 2048, "2K"),
)
_LARGEST_RESOLUTION_LABEL = "4K"

# Bounds what a single reservation can hold. A larger batch is still billed per
# image returned.
MAX_RESERVED_IMAGES = 10


class ImagePriceTier(BaseModel):
    """USD for one image at a resolution and quality the upstream names itself.

    Either axis may be absent when the upstream does not price along it.
    """

    resolution: str | None = None
    quality: str | None = None
    usd: float

    class Config:
        extra = "ignore"


class ImagePricing(BaseModel):
    """A model's per-image price book, in raw upstream USD."""

    max_usd: float
    tiers: list[ImagePriceTier] = []
    default_resolution: str | None = None
    default_quality: str | None = None
    resolutions: list[str] = []
    qualities: list[str] = []
    # Upscale factor to USD, for upstreams billing an upscale as its own call.
    upscale: dict[str, float] = {}

    class Config:
        extra = "ignore"

    def price_usd(
        self, resolution: str | None = None, quality: str | None = None
    ) -> float:
        """Price for one image at the requested tier.

        A request naming neither axis is priced at the upstream's defaults,
        which is what it will be charged; falling back to ``max_usd`` would
        bill a default 1K request at the 4K rate. An unmatched tier does fall
        back to ``max_usd``, under-billing being the worse error.
        """
        wanted_resolution = resolution or self.default_resolution
        wanted_quality = quality or self.default_quality

        for candidate in (
            (wanted_resolution, wanted_quality),
            (wanted_resolution, None),
        ):
            for tier in self.tiers:
                if tier.resolution == candidate[0] and tier.quality == candidate[1]:
                    return tier.usd

        return self.max_usd

    def upscale_usd(self, factor: str | None) -> float:
        """Price for one upscale at ``factor``, or the dearest one offered."""
        if factor and factor in self.upscale:
            return self.upscale[factor]
        return max(self.upscale.values()) if self.upscale else self.max_usd


def _resolution_from_size(body: dict) -> str | None:
    """Resolution label for an OpenAI-style ``size`` or explicit dimensions."""
    edges: list[int] = []
    size = body.get("size")
    if isinstance(size, str) and "x" in size.lower():
        for part in size.lower().split("x"):
            try:
                edges.append(int(part.strip()))
            except ValueError:
                return None
    else:
        for dimension in ("width", "height"):
            value = body.get(dimension)
            if isinstance(value, int) and not isinstance(value, bool):
                edges.append(value)
    if not edges:
        return None

    area = 1
    for edge in edges:
        area *= edge
    for limit, label in _RESOLUTION_AREAS:
        if area < limit:
            return label
    return _LARGEST_RESOLUTION_LABEL


def select_image_price_usd(image_pricing: ImagePricing, body: dict) -> float:
    """USD for one image, priced at the tier this request asks for.

    Reads the upstream's own ``resolution``/``quality`` first, then falls back
    to the OpenAI-compatible ``size`` (or explicit ``width``/``height``).
    """
    resolution = body.get("resolution")
    label = (
        resolution.upper()
        if isinstance(resolution, str) and resolution
        else _resolution_from_size(body)
    )
    quality = body.get("quality")
    quality_label = quality.lower() if isinstance(quality, str) and quality else None

    # A tier the model never declared is priced at the ceiling: reading it as
    # unspecified would bill it at the default tier, the cheaper one on every
    # model that has tiers at all.
    declared_resolutions = {r.upper() for r in image_pricing.resolutions}
    if label is not None and declared_resolutions and label not in declared_resolutions:
        return image_pricing.max_usd
    declared_qualities = {q.lower() for q in image_pricing.qualities}
    if (
        quality_label is not None
        and declared_qualities
        and quality_label not in declared_qualities
    ):
        return image_pricing.max_usd

    return image_pricing.price_usd(label, quality_label)


def per_image_sats(model: "Model | None", body: dict) -> float:
    """Sats for one image from this model, at the tier ``body`` asks for.

    ``sats_pricing.image`` is the ceiling, already carrying the provider fee
    and sats conversion, so a price book only scales it by the ratio of the
    selected tier to that ceiling.
    """
    if model is None or model.sats_pricing is None:
        return 0.0

    ceiling_sats = model.sats_pricing.image
    book = model.image_pricing
    if book is None or book.max_usd <= 0 or ceiling_sats <= 0:
        return ceiling_sats

    return ceiling_sats * select_image_price_usd(book, body) / book.max_usd


def produces_images(model: "Model | None") -> bool:
    architecture = getattr(model, "architecture", None)
    return getattr(architecture, "output_modalities", None) == ["image"]


def image_reservation_msats(body: dict, model: "Model | None") -> int | None:
    """Msats to hold for an image request, or ``None`` if not an image model.

    Token-window math means nothing for a model that returns images, so the
    hold is the requested tier times the requested batch size.
    """
    if not produces_images(model):
        return None

    sats_per_image = per_image_sats(model, body)
    if sats_per_image <= 0:
        return None

    try:
        count = int(body.get("n", 1))
    except (TypeError, ValueError):
        count = 1
    count = min(max(count, 1), MAX_RESERVED_IMAGES)

    return math.ceil(count * sats_per_image * 1000)
