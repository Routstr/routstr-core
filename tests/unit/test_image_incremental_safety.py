"""Incremental purchase guards atop the upstream endpoint/dispatch contract."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr import auth, proxy
from routstr.auth import ReservationSnapshot
from routstr.core.db import ApiKey, ReservationRelease
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.openrouter import OpenRouterUpstreamProvider

from .test_image_billing_units import TOKEN_BOOK, _model
from .test_image_endpoint_pinning import _endpoint, _per_image
from .test_image_endpoint_pinning import _model as endpoint_model
from .test_image_single_dispatch import IMAGE_MODEL, _request


def test_token_estimates_never_authorize_a_purchase() -> None:
    model = _model(TOKEN_BOOK)
    upstream = BaseUpstreamProvider("https://api.openai.com/v1", "key", 1.0)
    refused = proxy._price_image_candidate(
        {"prompt": "cat"}, model, upstream, "v1/images/generations"
    )
    assert refused == "Token-priced images require provider-enforced quantity bounds"


@pytest.mark.parametrize("fx", [0.002, 0.0005])
@pytest.mark.parametrize("router", [False, True])
@pytest.mark.asyncio
async def test_quote_and_settlement_share_fx_snapshot(fx: float, router: bool) -> None:
    model = (
        endpoint_model(_endpoint("cheap", _per_image(0.04))) if router else IMAGE_MODEL
    )
    provider = (
        OpenRouterUpstreamProvider("key", provider_fee=1.1)
        if router
        else BaseUpstreamProvider("https://api.venice.ai/api/v1", "key", 1.1)
    )
    with patch.object(proxy, "sats_usd_price", return_value=fx) as lookup:
        priced = proxy._price_image_candidate(
            {"prompt": "cat"}, model, provider, "v1/images/generations"
        )
    assert isinstance(priced, tuple)
    quoted = priced[0]
    assert quoted.sats_pricing is not None
    assert quoted.sats_pricing.image_output == pytest.approx(0.04 * 1.1 / fx)
    assert quoted._image_quote_usd_per_sat == fx
    assert model.sats_pricing is not None
    assert model.sats_pricing.image_output == 40.0
    assert "_image_quote_usd_per_sat" not in quoted.dict()
    lookup.assert_called_once()
    payload = {"data": [{"b64_json": "YQ=="}], "usage": {"cost": 0.04}}
    response = httpx.Response(200, json=payload)
    settle = AsyncMock(return_value={})
    with (
        patch("routstr.upstream.base.adjust_payment_for_tokens", settle),
        patch("routstr.payment.cost_calculation.sats_usd_price", return_value=fx * 2),
    ):
        await provider.handle_image_generation(
            response,
            ApiKey(hashed_key="key"),
            AsyncMock(),
            1000000,
            quoted,
            request_body=b'{"prompt":"cat"}',
            path="images/generations",
        )
    cost = settle.call_args.kwargs["precomputed_cost"]
    assert cost.total_usd == pytest.approx(0.044)


def test_openrouter_generation_route_maps_to_native_images() -> None:
    provider = OpenRouterUpstreamProvider("key")
    assert provider.normalize_request_path("v1/images/generations") == "images"
    assert provider.normalize_request_path("v1/chat/completions") == "chat/completions"
    model = endpoint_model(_endpoint("cheap", _per_image(0.04)))
    with patch.object(proxy, "sats_usd_price", return_value=0.001):
        assert isinstance(
            proxy._price_image_candidate(
                {"prompt": "cat"}, model, provider, "v1/images/edits"
            ),
            str,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_interrupted_charge_claim_releases_exact_image_reservation(
    cancel: bool,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = ApiKey(hashed_key="key", balance=100000)
        session.add(key)
        await session.commit()
        snapshot: ReservationSnapshot | None = None

        async def interrupted(*args: object) -> None:
            nonlocal snapshot
            candidate = args[8]
            assert isinstance(candidate, ReservationSnapshot)
            snapshot = candidate
            assert isinstance(snapshot, ReservationSnapshot)
            assert await auth._claim_reservation_for_charge(snapshot, session)
            if cancel:
                raise asyncio.CancelledError()
            raise RuntimeError("interrupted settlement")

        provider = BaseUpstreamProvider("https://api.venice.ai/api/v1", "key", 1.0)
        request = _request({"model": IMAGE_MODEL.id, "prompt": "cat"})
        with (
            patch.object(
                provider, "forward_request", AsyncMock(side_effect=interrupted)
            ),
            patch.object(
                proxy, "get_candidates", return_value=[(IMAGE_MODEL, provider)]
            ),
            patch.object(proxy, "sats_usd_price", return_value=0.001),
            patch.object(proxy, "get_bearer_token_key", AsyncMock(return_value=key)),
            patch.object(proxy, "check_token_balance"),
        ):
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                await proxy._proxy(
                    request, "v1/images/generations", session, await request.body()
                )
        await session.refresh(key)
        assert key.reserved_balance == 0
        assert key.balance == 100000
        assert snapshot is not None
        record = await session.get(ReservationRelease, snapshot.release_id)
        assert record is not None and record.status == "released"
    await engine.dispose()
