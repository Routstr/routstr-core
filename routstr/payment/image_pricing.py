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
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

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
    "ImageRequestRefused",
    "quote_image_endpoint",
    "with_image_book",
    "output_megapixels",
    "per_image_sats",
    "produces_images",
    "reference_image_count",
    "reference_images_sats",
    "requested_image_count",
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

# Most images one request may ask for. Settlement never charges past the
# reservation, so a larger batch is refused rather than served partly free.
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
    unit: ImageBillingUnit = "image"
    # ``token`` books: USD per image output token, and per input token by kind.
    output_token_usd: float = 0.0
    input_text_token_usd: float = 0.0
    input_image_token_usd: float = 0.0
    # Most tokens one reference image can meter on a ``token`` book. Without
    # it a request attaching reference images has no cost bound and is refused.
    max_input_image_tokens: int | None = None
    # ``megapixel`` books: USD per output megapixel at ``default_steps``.
    megapixel_usd: float = 0.0
    default_steps: int | None = None
    # USD per reference image the request attaches, past the first
    # ``input_images_included``; charged once per request, not per output.
    input_image_usd: float = 0.0
    input_images_included: int = 0
    # The upstream reports the USD it charged in ``usage.cost``; settle on it.
    trust_upstream_cost: bool = False

    # OpenRouter serves one model from several endpoints, each with its own
    # prices. Their books are keyed by ``provider_tag``; a request is quoted
    # on one and pinned to it, see ``quote_image_endpoint``.
    endpoints: dict[str, ImagePricing] = {}
    # On an endpoint's book: its tag, and the descriptor of every request
    # parameter it accepts (``{"type": "enum" | "range" | ..., ...}``).
    endpoint_tag: str | None = None
    parameters: dict[str, dict] = {}

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

    def input_ceiling_usd(self, body: dict) -> float | None:
        """Most USD the inputs of ``body`` can meter on a ``token`` book.

        The output side is bounded by the tier; this bounds the input side so
        the reservation is a ceiling, not an estimate. Text is bounded at one
        token per UTF-8 byte, which no BPE tokenizer exceeds. Reference images
        need ``max_input_image_tokens``; ``None`` means no bound exists.
        """
        if self.unit != "token":
            return 0.0
        text_bytes = sum(
            len(value.encode("utf-8"))
            for field in _TEXT_FIELDS
            if isinstance(value := body.get(field), str)
        )
        total = text_bytes * self.input_text_token_usd
        images = reference_image_count(body)
        if images > 0 and self.input_image_token_usd > 0:
            if self.max_input_image_tokens is None:
                return None
            total += images * self.max_input_image_tokens * self.input_image_token_usd
        return total


ImagePricing.update_forward_refs()


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
# Request fields a token-metered upstream tokenizes as text input.
_TEXT_FIELDS = ("prompt", "negative_prompt")


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


def per_image_sats(model: "Model | None", body: dict, path: str = "") -> float:
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
        factor = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    # A fractional factor has no key of its own; truncating it would price 2.5x
    # as 2x, so it falls through to the dearest upscale instead.
    if not factor.is_integer() or factor <= 0:
        return None
    return f"{int(factor)}x"


def reference_images_sats(model: "Model | None", body: dict) -> float:
    """Sats for the reference images ``body`` attaches, once per request."""
    if model is None or model.image_pricing is None:
        return 0.0
    return _sats_per_usd(model) * model.image_pricing.reference_usd(body)


def settle_image_sats(
    model: "Model | None", body: dict, usage: ImageUsage, path: str = ""
) -> float | None:
    """Sats to charge for what an image response actually carried.

    Preference order, each falling through when it has nothing to bill on:

    1. The upstream's own USD cost, when the book says to trust it.
    2. Reported image output tokens times the book's token rates.
    3. The per-image price at the requested tier, times images returned,
       plus the reference-image surcharge.

    Step 3 is the contract for ``image`` and ``megapixel`` books without
    ``trust_upstream_cost``, and for an endpoint's book that got no cost: it
    lists one per-image price, so that price is the bill. Any other book that
    trusts the upstream's cost but got none, or a ``token`` book whose response reports no usable tokens, cannot
    be metered; ``None`` tells the caller to release the reservation rather
    than bill an estimate as if it were authoritative.

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
        if book.trust_upstream_cost and book.endpoint_tag is None:
            return None
        if book.unit == "token":
            if usage.output_image_tokens <= 0:
                return None
            token_usd = book.token_usd(usage)
            return rate * token_usd if token_usd > 0 else None

    return usage.image_count * per_image_sats(
        model, body, path
    ) + reference_images_sats(model, body)


def requested_image_count(body: dict) -> int:
    """Images ``body`` asks for, refusing a batch the reservation cannot hold.

    OpenAI batches with ``n``, Venice's native route with ``variants``; either
    may be set and every returned image is billed, so the larger one counts.
    """
    count = 1
    for field in ("n", "variants"):
        try:
            count = max(count, int(body.get(field) or 1))
        except (TypeError, ValueError, OverflowError):
            continue
    if count > MAX_RESERVED_IMAGES:
        raise ImageRequestRefused(
            f"n and variants must be at most {MAX_RESERVED_IMAGES}"
        )
    return count


def produces_images(model: "Model | None") -> bool:
    architecture = getattr(model, "architecture", None)
    return getattr(architecture, "output_modalities", None) == ["image"]


def image_reservation_msats(
    body: dict, model: "Model | None", path: str = ""
) -> int | None:
    """Msats to hold for an image request, or ``None`` if it cannot be bounded.

    Token-window math means nothing for a model that returns images, so the
    hold is the requested tier times the requested batch size, plus the
    input ceiling of a ``token`` book. ``None`` refuses the request: a
    prepaid node must not buy a generation whose cost it cannot bound.
    """
    if model is None or not produces_images(model):
        return None

    book = model.image_pricing
    input_ceiling_usd = 0.0
    if book is not None:
        ceiling = book.input_ceiling_usd(body)
        if ceiling is None:
            return None
        input_ceiling_usd = ceiling
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

    try:
        count = requested_image_count(body)
    except ImageRequestRefused:
        return None

    total = (
        count * sats_per_image
        + reference_images_sats(model, body)
        + _sats_per_usd(model) * input_ceiling_usd
    )
    return math.ceil(total * 1000)


def with_image_book(model: "Model", book: ImagePricing) -> "Model":
    """``model`` priced on ``book`` instead, at the same sats per USD."""
    rate = _sats_per_usd(model)
    pricing = model.pricing.copy(
        update={"image_output": book.max_usd, "image": book.input_image_usd}
    )
    sats_pricing = (
        model.sats_pricing.copy(
            update={
                "image_output": rate * book.max_usd,
                "image": rate * book.input_image_usd,
            }
        )
        if model.sats_pricing is not None
        else None
    )
    return model.copy(
        update={
            "image_pricing": book,
            "pricing": pricing,
            "sats_pricing": sats_pricing,
        }
    )


class ImageRequestRefused(ValueError):
    """An image request that cannot be quoted, with the reason to report."""


# Fields an OpenRouter image request may carry. Anything else could change
# what the endpoint bills without the quote accounting for it.
_OPENROUTER_FIELDS = frozenset(
    {
        "model",
        "prompt",
        "stream",
        "n",
        "resolution",
        "aspect_ratio",
        "size",
        "quality",
        "output_format",
        "background",
        "output_compression",
        "seed",
        "input_references",
        "response_format",
        "user",
        "session_id",
    }
)
# Checked once per request rather than against an endpoint's descriptors;
# ``size`` is OpenAI-compatible input the router normalises itself.
_UNDESCRIBED_FIELDS = frozenset(
    {"model", "prompt", "stream", "user", "session_id", "response_format", "size"}
)
# ``size`` values that are an endpoint's ``resolution`` in shorthand.
_SIZE_TIERS = frozenset({"512", "768", "1K", "1.5K", "2K", "4K"})
_PIXEL_SIZE = re.compile(r"^\d{2,5}x\d{2,5}$")
_OUTPUT_FORMATS = frozenset({"png", "jpeg", "webp", "svg"})
_RESPONSE_FORMATS = frozenset({"b64_json", "url"})
_MAX_REQUEST_IMAGES = 10
_MAX_REFERENCES = 16
_MAX_ID_LENGTH = 256


def _check_references(refs: object) -> None:
    if not isinstance(refs, list) or len(refs) > _MAX_REFERENCES:
        raise ImageRequestRefused(
            f"input_references must be an array of at most {_MAX_REFERENCES} images"
        )
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or set(ref) != {"type", "image_url"}
            or ref["type"] != "image_url"
        ):
            raise ImageRequestRefused("References must be image_url content parts")
        image_url = ref["image_url"]
        if not isinstance(image_url, dict) or set(image_url) != {"url"}:
            raise ImageRequestRefused("References must contain image_url.url")
        url = image_url["url"]
        if not isinstance(url, str) or not url:
            raise ImageRequestRefused("Reference URL must be a nonempty string")
        try:
            parsed = urlsplit(url)
            web = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            web = False
        if not web and not (url.startswith("data:image/") and ";base64," in url):
            raise ImageRequestRefused(
                "References require HTTP(S) or base64 image data URLs"
            )


def _openrouter_parameters(body: dict) -> dict:
    """Check the request-level rules; return what each endpoint must accept."""
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageRequestRefused("prompt must be a nonempty string")
    if "provider" in body:
        raise ImageRequestRefused(
            "Client provider routing is not supported for image requests"
        )
    unknown = set(body) - _OPENROUTER_FIELDS
    if unknown:
        raise ImageRequestRefused(
            f"Unsupported image request fields: {', '.join(sorted(unknown))}"
        )
    n = body.get("n", 1)
    if (
        isinstance(n, bool)
        or not isinstance(n, int)
        or not 1 <= n <= _MAX_REQUEST_IMAGES
    ):
        raise ImageRequestRefused(
            f"n must be an integer between 1 and {_MAX_REQUEST_IMAGES}"
        )
    for field in ("user", "session_id"):
        value = body.get(field)
        if field in body and (
            not isinstance(value, str) or len(value) > _MAX_ID_LENGTH
        ):
            raise ImageRequestRefused(
                f"{field} must be a string of at most {_MAX_ID_LENGTH} characters"
            )
    output_format = body.get("output_format", "png")
    if not isinstance(output_format, str) or output_format not in _OUTPUT_FORMATS:
        raise ImageRequestRefused("Unsupported output_format")
    if body.get("background") == "transparent" and output_format not in {"png", "webp"}:
        raise ImageRequestRefused("Transparent background requires png or webp")
    if "response_format" in body and body["response_format"] not in _RESPONSE_FORMATS:
        raise ImageRequestRefused("Unsupported response_format")
    if "input_references" in body:
        _check_references(body["input_references"])

    parameters = {k: v for k, v in body.items() if k not in _UNDESCRIBED_FIELDS}
    size = body.get("size")
    if size is not None:
        if not isinstance(size, str):
            raise ImageRequestRefused("size must be a string")
        if size in _SIZE_TIERS:
            if "resolution" in body and body["resolution"] != size:
                raise ImageRequestRefused("size conflicts with resolution")
            parameters["resolution"] = size
        elif size != "auto" and not _PIXEL_SIZE.match(size):
            raise ImageRequestRefused("Unsupported size")
    return parameters


def _check_parameter(name: str, value: object, descriptors: dict[str, dict]) -> None:
    descriptor = descriptors.get(name)
    if descriptor is None:
        raise ImageRequestRefused(f"Endpoint does not support {name}")
    kind = descriptor.get("type")
    if kind == "enum":
        if not isinstance(value, str) or value not in descriptor.get("values", []):
            raise ImageRequestRefused(f"Unsupported {name}")
    elif kind == "range":
        count = (
            len(value)
            if name == "input_references" and isinstance(value, list)
            else value
        )
        low, high = descriptor.get("min"), descriptor.get("max")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or isinstance(low, bool)
            or not isinstance(low, (int, float))
            or isinstance(high, bool)
            or not isinstance(high, (int, float))
            or not low <= count <= high
        ):
            raise ImageRequestRefused(f"{name} is outside endpoint limits")
    elif kind == "boolean" and name == "seed":
        # The listing's boolean descriptor means "supported", not a boolean seed.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ImageRequestRefused("seed must be an integer")
    else:
        raise ImageRequestRefused(f"Cannot validate {name}")


def _check_endpoint(parameters: dict, n: int, descriptors: dict[str, dict]) -> None:
    for name, value in parameters.items():
        _check_parameter(name, value, descriptors)
    if "n" not in parameters and "n" in descriptors:
        _check_parameter("n", n, descriptors)


def quote_image_endpoint(body: dict, model: "Model", path: str = "") -> "Model":
    """``model`` quoted on the cheapest OpenRouter endpoint that accepts ``body``.

    The returned model carries that endpoint's own book, so the reservation
    and settlement use the prices of the endpoint the request is pinned to
    (``book.endpoint_tag``) rather than a blend of every endpoint's. Raises
    ``ImageRequestRefused`` with the first reason when no endpoint does.
    """
    book = model.image_pricing
    if book is None or not book.endpoints:
        raise ImageRequestRefused(
            "No image endpoint with bounded pricing is available for this model"
        )
    parameters = _openrouter_parameters(body)
    n = body.get("n", 1)
    quotes: list[tuple[int, str, "Model"]] = []
    reasons: list[str] = []
    for tag, endpoint_book in book.endpoints.items():
        try:
            _check_endpoint(parameters, n, endpoint_book.parameters)
        except ImageRequestRefused as refused:
            reasons.append(str(refused))
            continue
        quoted = with_image_book(model, endpoint_book)
        reserved = image_reservation_msats(body, quoted, path)
        if reserved is None:
            reasons.append("Image request cannot be priced on this endpoint")
            continue
        quotes.append((reserved, tag, quoted))
    if not quotes:
        raise ImageRequestRefused(reasons[0])
    return min(quotes, key=lambda quote: (quote[0], quote[1]))[2]
