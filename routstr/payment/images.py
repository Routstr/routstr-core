"""Bounded quotes and settlement for buffered image generation.

Endpoint pricing, not chat token rates, determines whether a request can be
reserved. Token/megapixel and variant-dependent prices deliberately fail closed
until their request limits can be enforced. A USD budget is a local admission
limit, never represented as an upstream spending cap.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from urllib.parse import urlsplit

from ..core.logging import get_logger
from .cost_calculation import CostData

logger = get_logger(__name__)


class ImageRequestError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ImageQuote:
    upstream_model_id: str
    provider_tag: str
    body_json: bytes
    upstream_max_usd: Decimal
    provider_fee: Decimal
    usd_per_sat: Decimal
    reserved_msats: int


def _error(message: str, code: str = "invalid_image_request") -> ImageRequestError:
    return ImageRequestError(code, message)


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise _error(f"{name} must be a finite nonnegative number")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise _error(f"{name} must be a finite nonnegative number") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise _error(
            f"{name} must be a finite {'positive' if positive else 'nonnegative'} number"
        )
    return result


def _mapping(value: object) -> dict:
    if hasattr(value, "dict"):
        value = value.dict()
    if not isinstance(value, dict):
        raise _error("Image endpoint metadata is invalid", "image_pricing_unbounded")
    return value


def _validate_parameter(name: str, value: object, descriptors: dict) -> None:
    if name not in descriptors:
        raise _error(f"Endpoint does not support {name}")
    desc = _mapping(descriptors[name])
    kind = desc.get("type")
    if kind == "enum":
        if not isinstance(value, str) or value not in desc.get("values", []):
            raise _error(f"Unsupported {name}")
    elif kind == "range":
        count = (
            len(value)
            if name == "input_references" and isinstance(value, list)
            else value
        )
        low, high = desc.get("min"), desc.get("max")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or isinstance(low, bool)
            or not isinstance(low, (int, float))
            or isinstance(high, bool)
            or not isinstance(high, (int, float))
            or not low <= count <= high
        ):
            raise _error(f"{name} is outside endpoint limits")
    elif kind == "boolean" and name == "seed":
        # Discovery's boolean descriptor means supported, not a boolean seed.
        if isinstance(value, bool) or not isinstance(value, int):
            raise _error("seed must be an integer")
    else:
        raise _error(f"Cannot validate {name}")


def _validate_references(refs: object) -> None:
    if not isinstance(refs, list) or len(refs) > 16:
        raise _error("input_references must be an array of at most 16 images")
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or set(ref) != {"type", "image_url"}
            or ref["type"] != "image_url"
        ):
            raise _error("References must be image_url content parts")
        image_url = ref["image_url"]
        if not isinstance(image_url, dict) or set(image_url) != {"url"}:
            raise _error("References must contain image_url.url")
        url = image_url["url"]
        if not isinstance(url, str) or not url:
            raise _error("Reference URL must be a nonempty string")
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            valid = False
        if not valid and not (url.startswith("data:image/") and ";base64," in url):
            raise _error("References require HTTP(S) or image base64 data URLs")


def quote_image_request(
    body: dict,
    *,
    upstream_model_id: str,
    capabilities: dict,
    provider_fee: float,
    usd_per_sat: float,
    max_request_usd: float,
) -> ImageQuote:
    """Validate a request and pin it to the cheapest safely bounded endpoint."""
    if not isinstance(body, dict):
        raise _error("Image request must be a JSON object")
    if not isinstance(upstream_model_id, str) or not upstream_model_id:
        raise _error("Image model is missing")
    if not isinstance(body.get("model"), str) or not body["model"]:
        raise _error("model must be a nonempty string")
    if not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
        raise _error("prompt must be a nonempty string")
    if "provider" in body:
        raise _error("Client provider routing is not supported for image requests")
    if "stream" in body and body["stream"] is not False:
        raise _error(
            "Only buffered image generation is supported", "image_streaming_unsupported"
        )
    allowed = {
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
        "user",
        "session_id",
    }
    if set(body) - allowed:
        raise _error("Unsupported image request fields")
    n = body.get("n", 1)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 10:
        raise _error("n must be an integer between 1 and 10")
    for field in ("user", "session_id"):
        if field in body and (
            not isinstance(body[field], str) or len(body[field]) > 256
        ):
            raise _error(f"{field} must be a string of at most 256 characters")
    if "output_format" in body and (
        not isinstance(body["output_format"], str)
        or body["output_format"] not in {"png", "jpeg", "webp", "svg"}
    ):
        raise _error("Unsupported output_format")
    if body.get("background") == "transparent" and body.get(
        "output_format", "png"
    ) not in {"png", "webp"}:
        raise _error("Transparent background requires png or webp")
    if "input_references" in body:
        _validate_references(body["input_references"])
    fee = _decimal(provider_fee, "provider fee", positive=True)
    fx = _decimal(usd_per_sat, "USD per sat", positive=True)
    budget = _decimal(max_request_usd, "image request budget", positive=True)
    cap = _mapping(capabilities)
    if "images" in cap:
        cap = _mapping(cap["images"])
    endpoints = cap.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise _error(
            "No image endpoint capabilities available", "image_pricing_unbounded"
        )
    # A routing tag pins a provider, not necessarily an individual endpoint
    # record. Multiple records under one tag make cheapest-record quoting
    # unsafe, even when their supported parameter sets differ.
    tag_counts: dict[str, int] = {}
    for raw_endpoint in endpoints:
        endpoint = _mapping(raw_endpoint)
        tag = endpoint.get("provider_tag")
        if isinstance(tag, str):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    candidates = []
    failures = []
    for raw_endpoint in endpoints:
        try:
            endpoint = _mapping(raw_endpoint)
            tag = endpoint.get("provider_tag")
            if not isinstance(tag, str) or not tag:
                raise _error("Endpoint cannot be pinned", "image_pricing_unbounded")
            if tag_counts[tag] != 1:
                raise _error(
                    "Provider tag does not uniquely identify endpoint pricing",
                    "image_pricing_unbounded",
                )
            descriptors = _mapping(endpoint.get("supported_parameters"))
            transformed = dict(body)
            # Tier shorthand has documented resolution equivalence. Arbitrary
            # pixels need provider normalization bounds and are not yet supported.
            if "size" in transformed:
                size = transformed.pop("size")
                if not isinstance(size, str) or size not in {
                    "512",
                    "768",
                    "1K",
                    "1.5K",
                    "2K",
                    "4K",
                }:
                    raise _error("Explicit pixel sizes are not yet safely supported")
                if "resolution" in transformed and transformed["resolution"] != size:
                    raise _error("size conflicts with resolution")
                transformed["resolution"] = size
            for name, value in transformed.items():
                if name in {"model", "prompt", "stream", "user", "session_id"}:
                    continue
                _validate_parameter(name, value, descriptors)
            if "n" not in transformed and "n" in descriptors:
                _validate_parameter("n", n, descriptors)
            lines = endpoint.get("pricing")
            if not isinstance(lines, list) or not lines:
                raise _error("Missing endpoint prices", "image_pricing_unbounded")
            upstream_cost = Decimal(0)
            output_price_found = False
            grouped: dict[tuple[str, str], Decimal] = {}
            units: dict[str, str] = {}
            for raw_line in lines:
                line = _mapping(raw_line)
                rate = _decimal(line.get("cost_usd"), "endpoint price")
                billable, unit = line.get("billable"), line.get("unit")
                if not isinstance(billable, str) or not isinstance(unit, str):
                    raise _error("Invalid pricing dimension", "image_pricing_unbounded")
                if billable in units and units[billable] != unit:
                    raise _error(
                        "Ambiguous endpoint pricing units", "image_pricing_unbounded"
                    )
                units[billable] = unit
                # Variants are alternatives, never additive. Reserve against
                # the maximum without inventing a universal tier mapping.
                grouped[(billable, unit)] = max(
                    grouped.get((billable, unit), Decimal(0)), rate
                )
            for (billable, unit), rate in grouped.items():
                if billable == "output_image" and unit == "image":
                    output_price_found = True
                    upstream_cost += rate * n
                elif billable in {"input_image", "input_reference"} and unit == "image":
                    upstream_cost += rate * len(body.get("input_references", []))
                elif rate == 0 and billable in {
                    "input_text",
                    "input_font",
                    "input_image",
                    "input_reference",
                }:
                    continue
                else:
                    raise _error(
                        "Endpoint pricing has no enforceable image quantity bound",
                        "image_pricing_unbounded",
                    )
            if not output_price_found:
                raise _error(
                    "No fixed per-image output price", "image_pricing_unbounded"
                )
            marked_up = upstream_cost * fee
            if marked_up > budget:
                raise _error(
                    "Image quote exceeds node request budget",
                    "image_request_budget_exceeded",
                )
            transformed["model"] = upstream_model_id
            transformed["stream"] = False
            transformed["provider"] = {"only": [tag], "allow_fallbacks": False}
            body_json = json.dumps(
                transformed, allow_nan=False, separators=(",", ":")
            ).encode()
            reserved = max(
                1,
                int((marked_up / fx * 1000).to_integral_value(rounding=ROUND_CEILING)),
            )
            candidates.append(
                ImageQuote(
                    upstream_model_id, tag, body_json, upstream_cost, fee, fx, reserved
                )
            )
        except ImageRequestError as exc:
            failures.append(exc)
    if not candidates:
        if failures:
            raise failures[0]
        raise _error("No safely priced endpoint", "image_pricing_unbounded")
    return min(
        candidates, key=lambda quote: (quote.upstream_max_usd, quote.provider_tag)
    )


def capability_is_quoteable(capabilities: dict) -> bool:
    """Whether any endpoint admits a bounded default text-to-image request.

    This is a catalogue eligibility check, not request authorization. The
    concrete request must still be quoted with the node's fee, FX and budget.
    """
    try:
        quote_image_request(
            {"model": "eligibility", "prompt": "eligibility"},
            upstream_model_id="eligibility",
            capabilities=capabilities,
            provider_fee=1.0,
            usd_per_sat=1.0,
            max_request_usd=float("1e100"),
        )
        return True
    except ImageRequestError:
        return False


def calculate_image_cost(response_data: dict, *, quote: ImageQuote) -> CostData:
    """Bill only a completed buffered result using its explicit upstream cost.

    Fee and FX are frozen at reservation time. An upstream cost exceeding the
    quoted ceiling is logged and never allowed to debit beyond the reservation.
    """
    if not isinstance(response_data, dict) or "error" in response_data:
        raise _error("Image generation did not complete", "image_response_invalid")
    created = response_data.get("created")
    if (
        isinstance(created, bool)
        or not isinstance(created, int)
        or created < 0
        or "type" in response_data
    ):
        raise _error(
            "Not a completed buffered image response", "image_response_invalid"
        )
    images = response_data.get("data")
    if not isinstance(images, list) or not images:
        raise _error(
            "Image generation returned no completed images", "image_response_invalid"
        )
    requested_n = json.loads(quote.body_json).get("n", 1)
    if len(images) > requested_n:
        raise _error(
            "Image response exceeds requested image count", "image_response_invalid"
        )
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("b64_json"), str):
            raise _error(
                "Image response lacks completed image bytes", "image_response_invalid"
            )
        try:
            if not base64.b64decode(image["b64_json"], validate=True):
                raise ValueError("empty image")
        except (ValueError, binascii.Error):
            raise _error(
                "Image response has invalid base64 image bytes",
                "image_response_invalid",
            ) from None
    usage = response_data.get("usage")
    if not isinstance(usage, dict) or "cost" not in usage:
        raise _error(
            "Image response has no authoritative usage cost", "image_cost_missing"
        )
    upstream = _decimal(usage["cost"], "upstream image cost")
    marked_up = upstream * quote.provider_fee
    msats = int(
        (marked_up / quote.usd_per_sat * 1000).to_integral_value(rounding=ROUND_CEILING)
    )
    if upstream > quote.upstream_max_usd or msats > quote.reserved_msats:
        logger.error(
            "Image upstream cost exceeded its reservation quote",
            extra={
                "model": quote.upstream_model_id,
                "provider_tag": quote.provider_tag,
                "reserved_msats": quote.reserved_msats,
                "reported_cost_msats": msats,
            },
        )
    charged = min(msats, quote.reserved_msats)
    return CostData(
        base_msats=0,
        input_msats=0,
        output_msats=charged,
        total_msats=charged,
        total_usd=float(min(marked_up, Decimal(charged) / 1000 * quote.usd_per_sat)),
        upstream_usd=float(upstream),
    )
