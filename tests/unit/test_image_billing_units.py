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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import routstr.auth as auth_module
from routstr.auth import ReservationSnapshot, get_reservation_snapshot, pay_for_request
from routstr.core.db import ApiKey, ReservationRelease
from routstr.payment.image_pricing import (
    ImagePriceTier,
    ImagePricing,
    ImageUsage,
    image_reservation_msats,
    output_megapixels,
    per_image_sats,
    reference_image_count,
    settle_image_sats,
)
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.base import IMAGES_PER_RESERVATION, BaseUpstreamProvider
from routstr.upstream.image_generation import read_image_response

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
)
TRUSTED_BOOK = ImagePricing(
    max_usd=1.0,
    tiers=[ImagePriceTier(usd=0.04)],
    unit="image",
    trust_upstream_cost=True,
)


def test_reservation_for_a_token_book_uses_the_per_image_estimate() -> None:
    model = _model(TOKEN_BOOK)
    assert per_image_sats(model, {}) == pytest.approx(10.0)
    assert per_image_sats(model, {"quality": "high"}) == pytest.approx(250.0)
    assert image_reservation_msats({"n": 2, "quality": "high"}, model) == 500_000


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


def test_token_book_falls_back_to_the_tier_without_usage() -> None:
    model = _model(TOKEN_BOOK)
    usage = ImageUsage(image_count=2)
    assert settle_image_sats(model, {"quality": "high"}, usage) == pytest.approx(500.0)


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
    usage = ImageUsage(image_count=3)
    assert settle_image_sats(model, {"resolution": "2K"}, usage) == pytest.approx(360.0)


def test_trusted_upstream_cost_wins_over_the_flat_rate() -> None:
    model = _model(TRUSTED_BOOK)
    usage = ImageUsage(image_count=1, upstream_cost_usd=0.011)
    assert settle_image_sats(model, {}, usage) == pytest.approx(11.0)
    # Without a reported cost the flat rate applies.
    assert settle_image_sats(model, {}, ImageUsage(image_count=2)) == pytest.approx(
        80.0
    )


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


def test_legacy_rows_read_image_as_the_output_ceiling() -> None:
    from routstr.core.db import ModelRow
    from routstr.payment.models import _row_to_model

    row = ModelRow(
        id="old",
        name="old",
        created=0,
        description="",
        context_length=0,
        architecture=json.dumps(
            {
                "modality": "text->image",
                "input_modalities": ["text"],
                "output_modalities": ["image"],
                "tokenizer": "Unknown",
                "instruct_type": None,
            }
        ),
        pricing=json.dumps({"prompt": 0.0, "completion": 0.0, "image": 0.01}),
        image_pricing=json.dumps({"max_usd": 0.01, "tiers": [{"usd": 0.01}]}),
    )
    with patch("routstr.payment.models.sats_usd_price", return_value=5.0e-4):
        model = _row_to_model(row)
    assert model.pricing.image_output == pytest.approx(0.01)
    assert model.pricing.image == 0.0
    assert model.sats_pricing is not None and model.sats_pricing.image_output > 0
    assert per_image_sats(model, {}) == pytest.approx(model.sats_pricing.image_output)


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
    model: Model, body: dict, payload: dict
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
        await pay_for_request(key, RESERVED, session)
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
            response = await provider.forward_request(
                request,
                "v1/images/generations",
                {},
                json.dumps(body).encode(),
                key,
                RESERVED,
                session,
                model,
                snapshot,
            )
            await _drain(response)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = await session.get(ApiKey, snapshot.key_hash)
        record = await session.get(ReservationRelease, snapshot.release_id)
        assert key is not None
        return key.balance, key.total_spent, record.status if record else None


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
    )
    expected_msats = math.ceil((1000 * 0.00003 + 10 * 0.000005) * 1000 * 1000)
    assert spent == expected_msats
    assert balance == BALANCE - expected_msats
    assert status == "charged"


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
    )
    expected_msats = math.ceil(0.0123 * 1000 * 1000)
    assert spent == expected_msats
    assert balance == BALANCE - expected_msats
    assert status == "charged"
