"""Image settlement across the three billing units and a trusted upstream cost.

A price book names how the upstream meters one generation. These tests pin
that the reservation is sized from the requested tier in every unit, and that
settlement reads the response for what was actually consumed: the upstream's
own USD cost first, then reported image output tokens, then a flat charge per
image returned.
"""

from __future__ import annotations

import json
import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic.v1 import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import routstr.auth as auth_module
from routstr.auth import ReservationSnapshot, get_reservation_snapshot, pay_for_request
from routstr.core.db import ApiKey, ReservationRelease
from routstr.core.exceptions import UpstreamError
from routstr.payment.cost_calculation import calculate_flat_cost
from routstr.payment.image_pricing import (
    ImagePriceTier,
    ImagePricing,
    ImageRequestRefused,
    ImageUsage,
    image_reservation_msats,
    output_megapixels,
    per_image_sats,
    reference_image_count,
    requested_image_count,
    settle_image_sats,
)
from routstr.payment.models import Architecture, Model, Pricing
from routstr.proxy import _price_image_candidate
from routstr.upstream.base import (
    IMAGES_PER_RESERVATION,
    BaseUpstreamProvider,
    _read_bounded,
)
from routstr.upstream.image_generation import read_image_response
from routstr.upstream.together import _override_book

BALANCE = 100_000
RESERVED = 5_000
# The ceiling is 1 USD so a book's USD figures read directly as sats ratios.
CEILING_SATS = 1_000.0


def _model(book: ImagePricing) -> Model:
    return Model(
        id="img",
        name="img",
        created=0,
        description="",
        context_length=0,
        architecture=Architecture(
            modality="text->image",
            input_modalities=["text"],
            output_modalities=["image"],
            tokenizer="Unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.0, completion=0.0, image_output=book.max_usd),
        sats_pricing=Pricing(prompt=0.0, completion=0.0, image_output=CEILING_SATS),
        image_pricing=book,
    )


TOKEN_BOOK = ImagePricing(
    max_usd=1.0,
    tiers=[
        ImagePriceTier(resolution="1K", quality="low", usd=0.01),
        ImagePriceTier(resolution="1K", quality="high", usd=0.25),
    ],
    default_resolution="1K",
    default_quality="low",
    resolutions=["1K"],
    qualities=["low", "high"],
    unit="token",
    output_token_usd=0.00003,
    input_text_token_usd=0.000005,
    input_image_token_usd=0.000008,
)
MEGAPIXEL_BOOK = ImagePricing(
    max_usd=1.0,
    tiers=[
        ImagePriceTier(resolution="1K", usd=0.03),
        ImagePriceTier(resolution="2K", usd=0.12),
    ],
    default_resolution="1K",
    resolutions=["1K", "2K"],
    unit="megapixel",
    megapixel_usd=0.03,
    default_steps=20,
)
TRUSTED_BOOK = ImagePricing(
    max_usd=1.0,
    tiers=[ImagePriceTier(usd=0.04)],
    unit="image",
    trust_upstream_cost=True,
)
# Venice's ``upscaler``: generation at 0.01, upscale priced by factor.
UPSCALE_BOOK = ImagePricing(
    max_usd=1.0,
    tiers=[ImagePriceTier(usd=0.01)],
    unit="image",
    upscale={"2x": 0.02, "4x": 0.08},
)


def test_upscale_is_priced_by_factor_not_generation_tier() -> None:
    model = _model(UPSCALE_BOOK)
    upscale = "v1/image/upscale"
    assert per_image_sats(model, {"prompt": "cat"}) == pytest.approx(10.0)
    assert per_image_sats(model, {"scale": 4}, upscale) == pytest.approx(80.0)
    assert per_image_sats(model, {"scale": "2x"}, upscale) == pytest.approx(20.0)
    # Venice upscales 2x when ``scale`` is left out.
    assert per_image_sats(model, {}, upscale) == pytest.approx(20.0)
    # An unknown factor reserves at the dearest upscale, not the generation price.
    assert per_image_sats(model, {"scale": 3}, upscale) == pytest.approx(80.0)
    # A fractional factor is unknown too, not truncated to the cheaper 2x.
    assert per_image_sats(model, {"scale": 2.5}, upscale) == pytest.approx(80.0)
    assert per_image_sats(model, {"scale": "2.5x"}, upscale) == pytest.approx(80.0)
    assert per_image_sats(model, {"scale": 4.0}, upscale) == pytest.approx(80.0)
    # ``scale`` on any other route is not an upscale.
    assert per_image_sats(model, {"scale": 2}, "v1/image/generate") == pytest.approx(
        10.0
    )
    assert image_reservation_msats({"scale": 4}, model, upscale) == 80_000
    usage = ImageUsage(image_count=1)
    assert settle_image_sats(model, {"scale": 4}, usage, upscale) == pytest.approx(80.0)


@pytest.mark.parametrize(
    ("body", "expected_images"),
    [
        ({"n": 3}, 3),
        ({"variants": 4}, 4),
        ({"n": 1e999}, 1),
        ({"n": "nope"}, 1),
        # Venice's native route batches by ``variants`` even when ``n`` is set.
        ({"n": 1, "variants": 2}, 2),
        ({"n": 4, "variants": 2}, 4),
        ({"n": "nope", "variants": 3}, 3),
        ({"n": 10}, 10),
    ],
)
def test_reservation_counts_openai_n_and_venice_variants(
    body: dict, expected_images: int
) -> None:
    model = _model(UPSCALE_BOOK)
    assert image_reservation_msats(body, model) == expected_images * 10_000


@pytest.mark.parametrize(
    "body", [{"n": 11}, {"variants": 11}, {"n": 1, "variants": 50}]
)
def test_a_batch_past_the_reservation_cap_is_refused(body: dict) -> None:
    with pytest.raises(ImageRequestRefused, match="at most 10"):
        requested_image_count(body)
    assert image_reservation_msats(body, _model(UPSCALE_BOOK)) is None


def test_the_proxy_refuses_an_oversized_batch_on_every_image_upstream() -> None:
    model = _model(UPSCALE_BOOK)
    upstream = MagicMock(base_url="https://api.venice.ai/api/v1", provider_fee=1.0)
    for body in ({"prompt": "cat", "n": 11}, {"prompt": "cat", "variants": 11}):
        refused = _price_image_candidate(body, model, upstream, "v1/image/generate")
        assert refused == "n and variants must be at most 10"
    with patch("routstr.proxy.sats_usd_price", return_value=0.001):
        priced = _price_image_candidate(
            {"prompt": "cat", "variants": 10}, model, upstream, "v1/image/generate"
        )
    assert isinstance(priced, tuple)
    assert priced[0].image_pricing == model.image_pricing
    assert priced[1] is upstream


def test_flat_cost_reports_usd_at_usd_per_sat() -> None:
    # The autouse fixture prices one sat at 5e-4 USD.
    cost = calculate_flat_cost(2, 10.0)
    assert cost.total_msats == 20_000
    assert cost.total_usd == pytest.approx(20 * 5.0e-4)


def test_together_override_rejects_an_unknown_unit() -> None:
    assert _override_book({"usd": 0.04, "unit": "pixel"}) is None
    assert _override_book({"usd": 0.04, "unit": "token"}) is None
    assert _override_book({"usd": 0.04, "unit": ["image"]}) is None
    megapixel = _override_book({"usd": 0.03, "unit": "megapixel"})
    assert megapixel is not None and megapixel.unit == "megapixel"
    flat = _override_book({"usd": 0.04})
    assert flat is not None and flat.unit == "image"
    with pytest.raises(ValidationError):
        ImagePricing.parse_obj({"max_usd": 0.04, "unit": "pixel"})


def test_reservation_for_a_token_book_uses_the_per_image_estimate() -> None:
    model = _model(TOKEN_BOOK)
    assert per_image_sats(model, {}) == pytest.approx(10.0)
    assert per_image_sats(model, {"quality": "high"}) == pytest.approx(250.0)
    assert image_reservation_msats({"n": 2, "quality": "high"}, model) == 500_000


def test_token_book_reservation_bounds_the_inputs() -> None:
    """The hold is a ceiling: tier per image, plus one text token per prompt
    byte and the book's per-image token cap for every reference image."""
    model = _model(TOKEN_BOOK.copy(update={"max_input_image_tokens": 1_000}))
    prompt = "a cat"  # 5 bytes -> 5 tokens at 0.000005 USD
    body = {"prompt": prompt, "quality": "high", "input_references": ["a", "b"]}
    text_usd = 5 * 0.000005
    images_usd = 2 * 1_000 * 0.000008
    expected_sats = 250.0 + (text_usd + images_usd) * CEILING_SATS
    assert image_reservation_msats(body, model) == math.ceil(expected_sats * 1000)
    # Enum strings such as ``quality`` are not prompt text.
    assert image_reservation_msats({"quality": "high"}, model) == 250_000


def test_token_book_refuses_reference_images_it_cannot_bound() -> None:
    model = _model(TOKEN_BOOK)
    assert TOKEN_BOOK.max_input_image_tokens is None
    assert image_reservation_msats({"prompt": "x"}, model) is not None
    assert image_reservation_msats({"prompt": "x", "image": "b64"}, model) is None


def test_token_book_settles_on_reported_tokens() -> None:
    model = _model(TOKEN_BOOK)
    usage = ImageUsage(
        image_count=1,
        input_text_tokens=100,
        input_image_tokens=0,
        output_image_tokens=4160,
    )
    expected_usd = 4160 * 0.00003 + 100 * 0.000005
    assert settle_image_sats(model, {}, usage) == pytest.approx(expected_usd * 1000)


def test_token_book_is_not_settled_on_the_tier_without_usage() -> None:
    """The tier is a reservation estimate, not a bill; without metering the
    caller releases the reservation instead."""
    model = _model(TOKEN_BOOK)
    usage = ImageUsage(image_count=2)
    assert settle_image_sats(model, {"quality": "high"}, usage) is None
    trusted = _model(TOKEN_BOOK.copy(update={"trust_upstream_cost": True}))
    assert settle_image_sats(trusted, {}, ImageUsage(image_count=1)) is None
    assert settle_image_sats(
        trusted, {}, ImageUsage(image_count=1, upstream_cost_usd=0.02)
    ) == pytest.approx(20.0)


def test_megapixel_book_prices_the_requested_area() -> None:
    model = _model(MEGAPIXEL_BOOK)
    assert output_megapixels({}) == 1.0
    assert output_megapixels({"size": "2048x2048"}) == pytest.approx(4.194304)
    assert output_megapixels({"width": 1024, "height": 768}) == pytest.approx(0.786432)
    assert output_megapixels({"resolution": "4K"}) == 16.0
    assert per_image_sats(model, {}) == pytest.approx(30.0)
    assert per_image_sats(model, {"resolution": "2K"}) == pytest.approx(120.0)
    assert per_image_sats(model, {"width": 1024, "height": 768}) == pytest.approx(
        30.0 * 0.786432
    )
    # Together only applies a steps multiplier above the catalog's default.
    assert per_image_sats(model, {"steps": 10}) == pytest.approx(30.0)
    assert per_image_sats(model, {"steps": 40}) == pytest.approx(60.0)
    usage = ImageUsage(image_count=3)
    assert settle_image_sats(model, {"resolution": "2K"}, usage) == pytest.approx(360.0)


def test_megapixel_book_rejects_custom_steps_without_a_known_default() -> None:
    book = MEGAPIXEL_BOOK.copy(update={"default_steps": None})
    assert image_reservation_msats({"steps": 40}, _model(book)) is None


def test_trusted_upstream_cost_wins_over_the_flat_rate() -> None:
    model = _model(TRUSTED_BOOK)
    usage = ImageUsage(image_count=1, upstream_cost_usd=0.011)
    assert settle_image_sats(model, {}, usage) == pytest.approx(11.0)
    # Without a reported cost there is nothing to bill on.
    assert settle_image_sats(model, {}, ImageUsage(image_count=2)) is None


def test_a_pinned_endpoint_without_a_reported_cost_bills_its_listed_price() -> None:
    """An endpoint lists one per-image price, so that price is the bill."""
    book = TRUSTED_BOOK.copy(update={"endpoint_tag": "alibaba"})
    model = _model(book)
    assert settle_image_sats(model, {}, ImageUsage(image_count=2)) == pytest.approx(
        80.0
    )
    usage = ImageUsage(image_count=1, upstream_cost_usd=0.011)
    assert settle_image_sats(model, {}, usage) == pytest.approx(11.0)


def test_nothing_returned_costs_nothing_even_with_a_reported_cost() -> None:
    model = _model(TRUSTED_BOOK)
    assert settle_image_sats(model, {}, ImageUsage(upstream_cost_usd=0.5)) == 0.0
    assert settle_image_sats(None, {}, ImageUsage(image_count=1)) == 0.0


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "data": [{"b64_json": "x"}],
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 1056,
                    "total_tokens": 1176,
                    "input_tokens_details": {"image_tokens": 100, "text_tokens": 20},
                },
            },
            ImageUsage(1, 20, 100, 1056, 0.0),
        ),
        (
            {
                "data": [{"b64_json": "x"}, {"b64_json": "y"}],
                "usage": {
                    "prompt_tokens": 16,
                    "completion_tokens": 272,
                    "total_tokens": 288,
                    "cost": 0.011,
                },
            },
            ImageUsage(2, 16, 0, 272, 0.011),
        ),
        (
            {
                "id": "x",
                "images": ["a"],
                "usage": {"cost_details": {"total_cost": "0.02"}},
            },
            ImageUsage(1, 0, 0, 0, 0.02),
        ),
        ({"data": [{"index": 0, "url": "http://x"}]}, ImageUsage(1)),
        ({"error": "nope"}, ImageUsage()),
        (b"not json", ImageUsage()),
    ],
)
def test_read_image_response_reads_both_usage_dialects(
    payload: Any, expected: ImageUsage
) -> None:
    content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    assert read_image_response(content, True) == expected


def test_raw_bytes_are_one_image() -> None:
    assert read_image_response(b"\x89PNG", False) == ImageUsage(image_count=1)
    assert read_image_response(b"", False) == ImageUsage()


def test_book_round_trips_its_unit_fields() -> None:
    restored = ImagePricing.parse_obj(json.loads(TOKEN_BOOK.json()))
    assert restored == TOKEN_BOOK
    # A book stored before units existed reads as a flat per-image one.
    legacy = ImagePricing.parse_obj({"max_usd": 0.04, "tiers": [{"usd": 0.04}]})
    assert legacy.unit == "image"
    assert legacy.trust_upstream_cost is False


def test_every_provider_reserves_a_batch_for_image_models() -> None:
    provider = BaseUpstreamProvider(base_url="http://u", api_key="k", provider_fee=1.5)
    priced = provider._apply_provider_fee_to_model(_model(TRUSTED_BOOK))
    assert priced.pricing.image_output == pytest.approx(1.5)
    assert priced.pricing.max_cost == pytest.approx(1.5 * IMAGES_PER_RESERVATION)
    assert priced.pricing.max_prompt_cost == 0.0


REFERENCE_BOOK = ImagePricing(
    max_usd=1.0,
    tiers=[ImagePriceTier(usd=0.04)],
    unit="image",
    input_image_usd=0.01,
    input_images_included=1,
)


def test_reference_images_are_charged_once_per_request() -> None:
    model = _model(REFERENCE_BOOK)
    body = {"n": 2, "input_references": ["a", "b", "c"]}
    assert reference_image_count(body) == 3
    assert (
        reference_image_count({"image_url": "http://x", "reference_images": ["a"]}) == 2
    )
    # Two past the included one, at 10 sats each, on top of two images.
    assert image_reservation_msats(body, model) == (2 * 40 + 20) * 1000
    assert settle_image_sats(model, body, ImageUsage(image_count=2)) == pytest.approx(
        100.0
    )
    assert settle_image_sats(model, {}, ImageUsage(image_count=1)) == pytest.approx(
        40.0
    )


# --- end to end through the proxy's image path ---------------------------------


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> Any:
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-4):
        yield


async def _engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    return engine


async def _drain(response: Any) -> bytes:
    body = b""
    if hasattr(response, "body_iterator"):
        async for chunk in response.body_iterator:
            body += chunk if isinstance(chunk, bytes) else chunk.encode()
    else:
        body = response.body
    return body


async def _settle(
    model: Model,
    body: dict,
    payload: dict,
    reserved: int = RESERVED,
    raises: str | None = None,
) -> tuple[int, int, str | None]:
    engine = await _engine()
    provider = BaseUpstreamProvider(
        base_url="http://upstream", api_key="k", provider_fee=1.0
    )
    upstream = httpx.Response(
        200,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "http://upstream"),
    )
    request = MagicMock()
    request.method = "POST"
    request.query_params = {}

    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = ApiKey(hashed_key="key", balance=BALANCE)
        session.add(key)
        await session.commit()
        await pay_for_request(key, reserved, session)
        snapshot: ReservationSnapshot = await get_reservation_snapshot(key, session)
        with (
            patch("httpx.AsyncClient.send", AsyncMock(return_value=upstream)),
            patch(
                "routstr.upstream.base.create_session",
                side_effect=lambda: AsyncSession(engine, expire_on_commit=False),
            ),
            patch(
                "routstr.upstream.base.adjust_payment_for_tokens",
                auth_module.adjust_payment_for_tokens,
            ),
        ):
            forwarded = provider.forward_request(
                request,
                "v1/images/generations",
                {},
                json.dumps(body).encode(),
                key,
                reserved,
                session,
                model,
                snapshot,
            )
            if raises:
                with pytest.raises(UpstreamError, match=raises):
                    await forwarded
            else:
                await _drain(await forwarded)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        settled = await session.get(ApiKey, snapshot.key_hash)
        record = await session.get(ReservationRelease, snapshot.release_id)
        assert settled is not None
        return settled.balance, settled.total_spent, record.status if record else None


@pytest.mark.asyncio
async def test_openai_style_response_is_billed_on_its_tokens() -> None:
    model = _model(TOKEN_BOOK)
    balance, spent, status = await _settle(
        model,
        {"model": "img", "prompt": "a cat", "quality": "low"},
        {
            "data": [{"b64_json": "x"}],
            "usage": {"input_tokens": 10, "output_tokens": 1000},
        },
        reserved=50_000,
    )
    expected_msats = math.ceil((1000 * 0.00003 + 10 * 0.000005) * 1000 * 1000)
    assert spent == expected_msats
    assert balance == BALANCE - expected_msats
    assert status == "charged"


@pytest.mark.asyncio
async def test_token_book_without_usage_releases_the_reservation() -> None:
    """No metering means no bill: an estimate is not charged."""
    model = _model(TOKEN_BOOK)
    balance, spent, _ = await _settle(
        model,
        {"model": "img", "prompt": "a cat", "quality": "low"},
        {"data": [{"b64_json": "x"}]},
    )
    assert spent == 0
    assert balance == BALANCE


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {"cost": 0}, {"completion_tokens": 272}])
async def test_trusted_book_without_reported_cost_releases_the_reservation(
    usage: dict | None,
) -> None:
    """A book that bills on the upstream's cost does not fall back to its tier."""
    payload: dict = {"data": [{"b64_json": "x"}]}
    if usage is not None:
        payload["usage"] = usage
    balance, spent, _ = await _settle(
        _model(TRUSTED_BOOK), {"model": "img", "prompt": "a cat"}, payload
    )
    assert spent == 0
    assert balance == BALANCE


@pytest.mark.asyncio
async def test_openrouter_style_response_is_billed_on_its_reported_cost() -> None:
    model = _model(TRUSTED_BOOK)
    balance, spent, status = await _settle(
        model,
        {"model": "img", "prompt": "a cat", "n": 2},
        {
            "data": [{"b64_json": "x"}, {"b64_json": "y"}],
            "usage": {"prompt_tokens": 16, "completion_tokens": 272, "cost": 0.0123},
        },
        reserved=50_000,
    )
    expected_msats = math.ceil(0.0123 * 1000 * 1000)
    assert spent == expected_msats
    assert balance == BALANCE - expected_msats
    assert status == "charged"


@pytest.mark.asyncio
async def test_settlement_never_charges_past_the_reservation() -> None:
    """An upstream billing more than it was quoted is the node's loss."""
    model = _model(TRUSTED_BOOK)
    balance, spent, status = await _settle(
        model,
        {"model": "img", "prompt": "a cat"},
        {"data": [{"b64_json": "x"}], "usage": {"cost": 0.0123}},
    )
    assert spent == RESERVED
    assert balance == BALANCE - RESERVED
    assert status == "charged"


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False])
async def test_an_oversized_image_response_is_refused(declared: bool) -> None:
    """Refused before it is buffered whole, whether or not it says its size."""
    payload = json.dumps({"data": [{"b64_json": "x" * 64}]}).encode()
    headers = {"content-type": "application/json"}
    if declared:
        headers["content-length"] = str(len(payload))
    response = httpx.Response(
        200,
        stream=httpx.ByteStream(payload),
        headers=headers,
        request=httpx.Request("POST", "http://upstream"),
    )
    with pytest.raises(UpstreamError, match="size limit"):
        await _read_bounded(response, len(payload) - 1)

    response = httpx.Response(
        200, content=payload, request=httpx.Request("POST", "http://upstream")
    )
    assert await _read_bounded(response, len(payload)) == payload


@pytest.mark.asyncio
async def test_an_oversized_image_response_charges_the_quote() -> None:
    """The upstream answered 200 and billed the node, so the hold is kept."""
    from routstr.core.settings import settings

    model = _model(TRUSTED_BOOK)
    with patch.object(settings, "image_max_response_bytes", 16):
        balance, spent, status = await _settle(
            model,
            {"model": "img", "prompt": "a cat"},
            {"data": [{"b64_json": "x" * 64}], "usage": {"cost": 0.001}},
            raises="size limit",
        )
    assert spent == RESERVED
    assert balance == BALANCE - RESERVED
    assert status == "charged"
