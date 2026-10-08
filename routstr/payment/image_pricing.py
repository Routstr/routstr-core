"""Per-image pricing for models that return images instead of tokens.

The same model costs a different amount per resolution, and the newer ones per
quality step within a resolution, so the price is only known once the request
is read.

``Pricing.image_output`` carries only the ceiling so it stays a flat numeric
record the provider-fee and sats conversions can map over. Tier prices here
are raw upstream USD and are applied as a ratio against that ceiling, which
is why neither conversion is repeated below. ``Pricing.image`` keeps its
OpenRouter meaning, USD per input image, which for generation is the
surcharge on each reference image the request attaches.

Upstreams bill images in three units. ``ImagePricing.unit`` names which one,
and ``settle_image_sats`` turns what the response carried into sats:

- ``image``: a flat price per image, tiered by resolution and quality.
- ``token``: OpenAI-style image output tokens, read from the response's
  ``usage``. Tiers then hold the per-image estimate used for the reservation.
- ``megapixel``: price times the output area the request asked for.

An upstream that reports its own USD cost in the response (OpenRouter) is
settled on that number when ``trust_upstream_cost`` is set, which is exact
regardless of unit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic.v1 import BaseModel

if TYPE_CHECKING:
    from .models import Model

__all__ = [
    "MAX_RESERVED_IMAGES",
    "ImagePriceTier",
    "ImagePricing",
    "ImageUsage",
    "ImageBillingUnit",
    "image_reservation_msats",
    "output_megapixels",
    "per_image_sats",
    "produces_images",
    "reference_image_count",
    "reference_images_sats",
    "resolution_label",
    "select_image_price_usd",
    "settle_image_sats",
]

ImageBillingUnit = Literal["image", "token", "megapixel"]

# 1K is nominally 1024x1024, 2K is 2048x2048. A pixel size maps onto a class by
# area, bounded at twice the nominal area so a class's portrait and landscape
# variants (1024x1536) stay in it. A size exactly on a boundary takes the
# dearer class rather than under-billing.
_RESOLUTION_AREAS: tuple[tuple[int, str], ...] = (
    (2 * 1024 * 1024, "1K"),
    (2 * 2048 * 2048, "2K"),
)
_LARGEST_RESOLUTION_LABEL = "4K"

# Nominal output area per resolution class, for upstreams billing by area
# when the request names a class instead of a pixel size.
RESOLUTION_MEGAPIXELS: dict[str, float] = {
    "512": 0.25,
    "1K": 1.0,
    "2K": 4.0,
    "4K": 16.0,
}
_DEFAULT_MEGAPIXELS = 1.0

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
    """A model's per-image price book, in raw upstream USD.

    ``max_usd`` is the ceiling one image can cost and is what
    ``Pricing.image_output`` mirrors. Every other USD figure here is converted
    to sats by its ratio to that ceiling, so the book never needs the exchange
    rate or provider fee.
    """

    max_usd: float
    tiers: list[ImagePriceTier] = []
    default_resolution: str | None = None
    default_quality: str | None = None
    resolutions: list[str] = []
    qualities: list[str] = []
    # Upscale factor to USD, for upstreams billing an upscale as its own call.
    upscale: dict[str, float] = {}

    # How the upstream meters one generation. Tiers still describe the
    # per-image estimate for token and megapixel books, since the reservation
    # is taken before the response says how much was actually consumed.
    unit: str = "image"
    # ``token`` books: USD per image output token, and per input token by kind.
    output_token_usd: float = 0.0
    input_text_token_usd: float = 0.0
    input_image_token_usd: float = 0.0
    # ``megapixel`` books: USD per output megapixel at ``default_steps``.
    megapixel_usd: float = 0.0
    default_steps: int | None = None
    # USD per reference image the request attaches, past the first
    # ``input_images_included``; charged once per request, not per output.
    input_image_usd: float = 0.0
    input_images_included: int = 0
    # The upstream reports the USD it charged in ``usage.cost``; settle on it.
    trust_upstream_cost: bool = False

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

    def reference_usd(self, body: dict) -> float:
        """Surcharge for the reference images ``body`` attaches."""
        if self.input_image_usd <= 0:
            return 0.0
        billable = reference_image_count(body) - self.input_images_included
        return max(billable, 0) * self.input_image_usd

    def token_usd(self, usage: "ImageUsage") -> float:
        """USD for the tokens a ``token`` book's response reports."""
        return (
            usage.output_image_tokens * self.output_token_usd
            + usage.input_text_tokens * self.input_text_token_usd
            + usage.input_image_tokens * self.input_image_token_usd
        )


@dataclass(frozen=True)
class ImageUsage:
    """What an image response carried, as far as billing is concerned.

    ``upstream_cost_usd`` is the USD the upstream says it charged, when it
    says so at all. Token counts are zero for upstreams that report none.
    """

    image_count: int = 0
    input_text_tokens: int = 0
    input_image_tokens: int = 0
    output_image_tokens: int = 0
    upstream_cost_usd: float = 0.0


# Request fields that carry reference images, across the supported dialects:
# OpenRouter ``input_references``, Together ``reference_images``/``image_url``,
# Venice ``style_references``, OpenAI edits ``image``.
_REFERENCE_FIELDS = (
    "input_references",
    "reference_images",
    "style_references",
    "image",
    "image_url",
)


def reference_image_count(body: dict) -> int:
    """How many reference images a request attaches."""
    count = 0
    for field in _REFERENCE_FIELDS:
        value = body.get(field)
        if isinstance(value, list):
            count += len(value)
        elif isinstance(value, (str, dict)) and value:
            count += 1
    return count


def _edges(body: dict) -> list[int]:
    """Pixel edges from an OpenAI-style ``size`` or explicit dimensions."""
    edges: list[int] = []
    size = body.get("size")
    if isinstance(size, str) and "x" in size.lower():
        for part in size.lower().split("x"):
            try:
                edges.append(int(part.strip()))
            except ValueError:
                return []
        return edges
    for dimension in ("width", "height"):
        value = body.get(dimension)
        if isinstance(value, int) and not isinstance(value, bool):
            edges.append(value)
    return edges


def _resolution_from_size(body: dict) -> str | None:
    """Resolution label for an OpenAI-style ``size`` or explicit dimensions."""
    edges = _edges(body)
    if not edges:
        return None

    area = 1
    for edge in edges:
        area *= edge
    for limit, label in _RESOLUTION_AREAS:
        if area < limit:
            return label
    return _LARGEST_RESOLUTION_LABEL


def resolution_label(body: dict) -> str | None:
    """The resolution class a request asks for, upper-cased, or ``None``.

    Reads the upstream's own ``resolution`` first, then falls back to the
    OpenAI-compatible ``size`` (or explicit ``width``/``height``).
    """
    resolution = body.get("resolution")
    if isinstance(resolution, str) and resolution:
        return resolution.upper()
    return _resolution_from_size(body)


def output_megapixels(body: dict) -> float:
    """Output area in megapixels a request asks for, defaulting to 1MP.

    A pixel size wins over a resolution class; a request naming neither is
    assumed to produce the 1K default most upstreams use.
    """
    edges = _edges(body)
    if len(edges) == 2:
        return edges[0] * edges[1] / 1_000_000
    if len(edges) == 1:
        return edges[0] * edges[0] / 1_000_000
    label = resolution_label(body)
    if label is not None and label in RESOLUTION_MEGAPIXELS:
        return RESOLUTION_MEGAPIXELS[label]
    return _DEFAULT_MEGAPIXELS


def select_image_price_usd(image_pricing: ImagePricing, body: dict) -> float:
    """USD for one image, priced at the tier this request asks for."""
    label = resolution_label(body)
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


def _sats_per_usd(model: "Model") -> float:
    """Sats one raw upstream dollar converts to for this model, or zero.

    ``sats_pricing.image`` already carries the provider fee and the exchange
    rate for ``image_pricing.max_usd``, so their ratio converts any USD
    figure from the same book.
    """
    if model.sats_pricing is None or model.image_pricing is None:
        return 0.0
    ceiling_sats = model.sats_pricing.image_output
    max_usd = model.image_pricing.max_usd
    if ceiling_sats <= 0 or max_usd <= 0:
        return 0.0
    return ceiling_sats / max_usd


def per_image_sats(
    model: "Model | None", body: dict, path: str = ""
) -> float:
    """Sats for one image from this model, at the tier ``body`` asks for.

    ``sats_pricing.image_output`` is the ceiling, already carrying the
    provider fee and sats conversion, so a price book only scales it by the
    ratio of the selected tier to that ceiling. A megapixel book prices the
    requested area directly, since its tiers are only estimates for nominal
    sizes. Reference-image surcharges are per request; see
    ``reference_images_sats``.
    """
    if model is None or model.sats_pricing is None:
        return 0.0

    ceiling_sats = model.sats_pricing.image_output
    book = model.image_pricing
    if book is None or book.max_usd <= 0 or ceiling_sats <= 0:
        return ceiling_sats

    rate = ceiling_sats / book.max_usd
    # An upscale is priced by factor, not tier; Venice defaults ``scale`` to 2.
    if book.upscale and path.rstrip("/").endswith("image/upscale"):
        return rate * book.upscale_usd(_scale_factor(body.get("scale", 2)))
    if book.unit == "megapixel" and book.megapixel_usd > 0:
        return (
            rate
            * output_megapixels(body)
            * book.megapixel_usd
            * _steps_multiplier(body.get("steps"), book.default_steps)
        )
    return rate * select_image_price_usd(book, body)


def _steps_multiplier(value: object, default_steps: int | None) -> float:
    """Together's per-MP multiplier above the catalog's default step count."""
    if default_steps is None or default_steps <= 0 or isinstance(value, bool):
        return 1.0
    try:
        steps = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 1.0
    if not math.isfinite(steps) or steps <= default_steps:
        return 1.0
    return steps / default_steps


def _scale_factor(value: object) -> str | None:
    """``4`` or ``"4x"`` as the book's ``"4x"`` key; ``None`` when unreadable."""
    if isinstance(value, str):
        value = value.lower().removesuffix("x")
    try:
        factor = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return f"{factor}x" if factor > 0 else None


def reference_images_sats(model: "Model | None", body: dict) -> float:
    """Sats for the reference images ``body`` attaches, once per request."""
    if model is None or model.image_pricing is None:
        return 0.0
    return _sats_per_usd(model) * model.image_pricing.reference_usd(body)


def settle_image_sats(
    model: "Model | None", body: dict, usage: ImageUsage, path: str = ""
) -> float:
    """Sats to charge for what an image response actually carried.

    Preference order, each falling through when it has nothing to bill on:

    1. The upstream's own USD cost, when the book says to trust it.
    2. Reported image output tokens times the book's token rates.
    3. The per-image price at the requested tier, times images returned,
       plus the reference-image surcharge.

    A response with no images and no reported cost is free; the reservation
    is released instead.
    """
    if model is None or usage.image_count <= 0:
        return 0.0

    book = model.image_pricing
    rate = _sats_per_usd(model)

    if book is not None and rate > 0:
        if book.trust_upstream_cost and usage.upstream_cost_usd > 0:
            return rate * usage.upstream_cost_usd
        if book.unit == "token" and usage.output_image_tokens > 0:
            token_usd = book.token_usd(usage)
            if token_usd > 0:
                return rate * token_usd

    return usage.image_count * per_image_sats(
        model, body, path
    ) + reference_images_sats(model, body)


def produces_images(model: "Model | None") -> bool:
    architecture = getattr(model, "architecture", None)
    return getattr(architecture, "output_modalities", None) == ["image"]


def image_reservation_msats(
    body: dict, model: "Model | None", path: str = ""
) -> int | None:
    """Msats to hold for an image request, or ``None`` if not an image model.

    Token-window math means nothing for a model that returns images, so the
    hold is the requested tier times the requested batch size.
    """
    if not produces_images(model):
        return None

    book = getattr(model, "image_pricing", None)
    # A per-MP rate is tied to a provider's default step count. If a caller
    # changes ``steps`` but the catalog did not publish that default, no exact
    # reservation is possible, so fail closed rather than undercharge.
    if (
        book is not None
        and book.unit == "megapixel"
        and "steps" in body
        and book.default_steps is None
    ):
        return None

    sats_per_image = per_image_sats(model, body, path)
    if sats_per_image <= 0:
        return None

    # OpenAI batches with ``n``; Venice's native route with ``variants``.
    try:
        count = int(body.get("n") or body.get("variants") or 1)
    except (TypeError, ValueError, OverflowError):
        count = 1
    count = min(max(count, 1), MAX_RESERVED_IMAGES)

    total = count * sats_per_image + reference_images_sats(model, body)
    return math.ceil(total * 1000)
