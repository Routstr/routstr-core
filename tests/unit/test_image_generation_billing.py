"""Image-generation endpoints are billed per image returned.

Image responses carry no usage object, so the generic non-chat path released
the reservation and served them free. These tests pin the flat per-image
settlement that replaces it, and that a response producing none costs nothing.
"""

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
from routstr import proxy as proxy_module
from routstr.auth import ReservationSnapshot, get_reservation_snapshot, pay_for_request
from routstr.core.db import ApiKey, ModelRow, ReservationRelease
from routstr.payment.helpers import calculate_discounted_max_cost
from routstr.payment.image_pricing import ImagePriceTier, ImagePricing
from routstr.payment.models import Architecture, Model, Pricing, _row_to_model
from routstr.upstream.base import BaseUpstreamProvider

from .proxy_test_utils import mock_request_stream, patch_proxy_session

BALANCE = 100_000
RESERVED = 5_000
SATS_PER_IMAGE = 1.0

IMAGE_MODEL = Model(
    id="venice-sd35",
    name="Venice SD35",
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
    pricing=Pricing(prompt=0.0, completion=0.0, image_output=0.0005),
    sats_pricing=Pricing(prompt=0.0, completion=0.0, image_output=SATS_PER_IMAGE),
)
UNPRICED_MODEL = IMAGE_MODEL.copy(
    update={"sats_pricing": Pricing(prompt=0.0, completion=0.0, image_output=0.0)}
)

# Ceiling $0.40, default tier 1K/medium at $0.10, cheapest 1K/low at $0.04.
PRICE_BOOK = ImagePricing(
    max_usd=0.4,
    tiers=[
        ImagePriceTier(resolution="1K", quality="low", usd=0.04),
        ImagePriceTier(resolution="1K", quality="medium", usd=0.1),
        ImagePriceTier(resolution="2K", quality="high", usd=0.4),
    ],
    default_resolution="1K",
    default_quality="medium",
    resolutions=["1K", "2K"],
    qualities=["low", "medium", "high"],
)
TIERED_MODEL = IMAGE_MODEL.copy(update={"image_pricing": PRICE_BOOK})

BODY = {"model": IMAGE_MODEL.id, "prompt": "a cat"}


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> Any:
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-4):
        yield


async def _engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    return engine


def _upstream(content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("POST", "http://upstream"),
    )


async def _drain(response: Any) -> bytes:
    body = b""
    if hasattr(response, "body_iterator"):
        async for chunk in response.body_iterator:
            body += chunk if isinstance(chunk, bytes) else chunk.encode()
    else:
        body = response.body
    return body


async def _forward(
    engine: AsyncEngine,
    path: str,
    upstream: httpx.Response,
    model: Model = IMAGE_MODEL,
    body: dict | None = None,
) -> tuple[bytes, ReservationSnapshot]:
    provider = BaseUpstreamProvider(
        base_url="http://upstream", api_key="k", provider_fee=1.0
    )
    request = MagicMock()
    request.method = "POST"
    request.query_params = {}
    send = AsyncMock(return_value=upstream)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = ApiKey(hashed_key="key", balance=BALANCE)
        session.add(key)
        await session.commit()
        await pay_for_request(key, RESERVED, session)
        snapshot = await get_reservation_snapshot(key, session)

        with (
            patch("httpx.AsyncClient.send", send),
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
                path,
                {},
                json.dumps(body if body is not None else BODY).encode(),
                key,
                RESERVED,
                session,
                model,
                snapshot,
            )
            out = await _drain(response)
    return out, snapshot


async def _ledger(
    engine: AsyncEngine, snapshot: ReservationSnapshot
) -> tuple[int, int, int, str | None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = await session.get(ApiKey, snapshot.key_hash)
        record = await session.get(ReservationRelease, snapshot.release_id)
        assert key is not None
        return (
            key.balance,
            key.total_spent,
            key.reserved_balance,
            record.status if record else None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "expected_images"),
    [
        ("v1/images/generations", {"data": [{"b64_json": "a"}, {"b64_json": "b"}]}, 2),
        ("v1/image/generate", {"id": "gen-1", "images": ["a", "b", "c"]}, 3),
        ("openai/v1/images/edits", {"data": [{"url": "http://x"}]}, 1),
    ],
)
async def test_images_are_charged_per_returned_image(
    path: str, payload: dict, expected_images: int
) -> None:
    engine = await _engine()
    out, snapshot = await _forward(
        engine, path, _upstream(json.dumps(payload).encode(), "application/json")
    )

    assert json.loads(out) == payload
    balance, spent, reserved, status = await _ledger(engine, snapshot)
    expected_msats = int(expected_images * SATS_PER_IMAGE * 1000)
    assert spent == expected_msats
    assert balance == BALANCE - expected_msats
    assert reserved == 0
    assert status == "charged"


@pytest.mark.asyncio
async def test_binary_image_response_counts_as_one_image() -> None:
    engine = await _engine()
    out, snapshot = await _forward(
        engine, "v1/image/upscale", _upstream(b"\x89PNG\r\n", "image/png")
    )

    assert out == b"\x89PNG\r\n"
    _, spent, reserved, _ = await _ledger(engine, snapshot)
    assert spent == int(SATS_PER_IMAGE * 1000)
    assert reserved == 0


@pytest.mark.asyncio
async def test_empty_image_response_is_not_charged() -> None:
    engine = await _engine()
    _, snapshot = await _forward(
        engine,
        "v1/images/generations",
        _upstream(json.dumps({"data": []}).encode(), "application/json"),
    )

    balance, spent, reserved, _ = await _ledger(engine, snapshot)
    assert spent == 0
    assert balance == BALANCE
    assert reserved == 0


@pytest.mark.asyncio
async def test_model_without_a_per_image_rate_is_not_charged() -> None:
    engine = await _engine()
    _, snapshot = await _forward(
        engine,
        "v1/images/generations",
        _upstream(
            json.dumps({"data": [{"b64_json": "a"}]}).encode(), "application/json"
        ),
        model=UNPRICED_MODEL,
    )

    balance, spent, reserved, _ = await _ledger(engine, snapshot)
    assert spent == 0
    assert balance == BALANCE
    assert reserved == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_usd"),
    [
        # No tier named: the model's own default, not its ceiling.
        ({"model": IMAGE_MODEL.id, "prompt": "a cat"}, 0.1),
        ({"model": IMAGE_MODEL.id, "resolution": "1K", "quality": "low"}, 0.04),
        ({"model": IMAGE_MODEL.id, "resolution": "2K", "quality": "high"}, 0.4),
        # A tier the model never declared is billed at the ceiling.
        ({"model": IMAGE_MODEL.id, "resolution": "4K"}, 0.4),
    ],
)
async def test_charge_uses_the_tier_the_request_asked_for(
    body: dict, expected_usd: float
) -> None:
    engine = await _engine()
    _, snapshot = await _forward(
        engine,
        "v1/images/generations",
        _upstream(
            json.dumps({"data": [{"b64_json": "a"}]}).encode(), "application/json"
        ),
        model=TIERED_MODEL,
        body=body,
    )

    # sats_pricing.image_output is the ceiling in sats; a tier scales against max_usd.
    expected_msats = math.ceil(
        SATS_PER_IMAGE * (expected_usd / PRICE_BOOK.max_usd) * 1000
    )
    _, spent, reserved, _ = await _ledger(engine, snapshot)
    assert spent == expected_msats
    assert reserved == 0


@pytest.mark.asyncio
async def test_reservation_holds_the_requested_tier_not_the_ceiling() -> None:
    ceiling = await calculate_discounted_max_cost(
        999_999,
        {"model": TIERED_MODEL.id, "resolution": "2K", "quality": "high"},
        model_obj=TIERED_MODEL,
    )
    cheapest = await calculate_discounted_max_cost(
        999_999,
        {"model": TIERED_MODEL.id, "resolution": "1K", "quality": "low"},
        model_obj=TIERED_MODEL,
    )

    assert ceiling == math.ceil(SATS_PER_IMAGE * 1000)
    assert cheapest == math.ceil(SATS_PER_IMAGE * (0.04 / 0.4) * 1000)


def test_price_book_survives_a_database_round_trip() -> None:
    row = ModelRow(
        id=TIERED_MODEL.id,
        upstream_provider_id=1,
        name=TIERED_MODEL.name,
        created=0,
        description="",
        context_length=0,
        architecture=TIERED_MODEL.architecture.json(),
        pricing=TIERED_MODEL.pricing.json(),
        image_pricing=PRICE_BOOK.json(),
    )

    restored = _row_to_model(row).image_pricing

    assert restored is not None
    assert restored.max_usd == pytest.approx(0.4)
    assert restored.default_resolution == "1K"
    assert restored.default_quality == "medium"
    assert [(t.resolution, t.quality, t.usd) for t in restored.tiers] == [
        ("1K", "low", 0.04),
        ("1K", "medium", 0.1),
        ("2K", "high", 0.4),
    ]


@pytest.mark.asyncio
async def test_reservation_is_sized_from_the_requested_batch() -> None:
    single = await calculate_discounted_max_cost(
        999_999, {"model": IMAGE_MODEL.id}, model_obj=IMAGE_MODEL
    )
    batch = await calculate_discounted_max_cost(
        999_999, {"model": IMAGE_MODEL.id, "n": 3}, model_obj=IMAGE_MODEL
    )

    assert single == int(SATS_PER_IMAGE * 1000)
    assert batch == int(3 * SATS_PER_IMAGE * 1000)


# A chat model the upstream can also serve on the images API (OpenRouter's
# google/gemini-2.5-flash-image): its ``image_output`` is a per-token rate.
CHAT_IMAGE_MODEL = IMAGE_MODEL.copy(
    update={
        "architecture": IMAGE_MODEL.architecture.copy(
            update={"output_modalities": ["image", "text"]}
        ),
        "sats_pricing": Pricing(prompt=0.0003, completion=0.0025, image_output=0.03),
    }
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "extra"),
    [
        (UNPRICED_MODEL, {}),
        (CHAT_IMAGE_MODEL, {}),
        (IMAGE_MODEL, {"stream": True}),
    ],
)
async def test_image_route_refuses_what_it_cannot_bill(
    model: Model, extra: dict
) -> None:
    upstream = MagicMock(forward_request=AsyncMock(), base_url="https://api.example")
    request = MagicMock(method="POST", headers={"authorization": "Bearer sk-x"})
    request.state.request_id = "req-image"
    mock_request_stream(request, json.dumps({**BODY, **extra}).encode())

    with (
        patch.object(proxy_module, "get_candidates", return_value=[(model, upstream)]),
        patch_proxy_session(MagicMock()),
    ):
        response = await proxy_module.proxy(request, "v1/images/generations")

    assert response.status_code == 400
    upstream.forward_request.assert_not_awaited()
