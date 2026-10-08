"""Actual account reservation/settlement with a mocked OpenRouter image transport."""

import asyncio
import base64
import hashlib
import json
import time
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.requests import Request

from routstr.core.db import ApiKey, ReservationRelease
from routstr.modalities import ApiCapability
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream import images
from routstr.upstream.openrouter import OpenRouterUpstreamProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True, "cancelled"])
@pytest.mark.parametrize("via_route", [False, True])
async def test_image_account_real_reservation(
    integration_session: AsyncSession,
    integration_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    failed: bool | str,
    via_route: bool,
) -> None:
    import routstr.proxy as proxy

    session = integration_session
    key = ApiKey(
        hashed_key=hashlib.sha256(b"buffered-image-test").hexdigest(), balance=1000000
    )
    session.add(key)
    await session.commit()
    model = Model(
        id="image-model",
        name="image",
        created=1,
        description="",
        context_length=0,
        architecture=Architecture(
            modality="text->image",
            input_modalities=["text"],
            output_modalities=["image"],
            tokenizer="unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0, completion=0),
        api_capabilities={
            "images": ApiCapability.parse_obj(
                {
                    "fetched_at": int(time.time()),
                    "endpoints": [
                        {
                            "provider_slug": "recraft",
                            "provider_tag": "recraft",
                            "supported_parameters": {},
                            "pricing": [
                                {
                                    "billable": "output_image",
                                    "unit": "image",
                                    "cost_usd": 0.007,
                                }
                            ],
                        }
                    ],
                }
            )
        },
    )
    upstream = OpenRouterUpstreamProvider("dummy-provider-key", provider_fee=1.1)
    monkeypatch.setattr(proxy, "get_candidates", lambda _: [(model, upstream)])

    async def authenticated_key(
        headers: dict[str, str],
        path: str,
        request_session: AsyncSession,
        authorization: str,
        **kwargs: Any,
    ) -> ApiKey | None:
        return await request_session.get(ApiKey, key.hashed_key)

    monkeypatch.setattr(proxy, "get_bearer_token_key", authenticated_key)
    monkeypatch.setattr(images.settings, "image_generation_enabled", True)
    monkeypatch.setattr(images.settings, "image_max_request_usd", 1.0)
    monkeypatch.setattr(images, "sats_usd_price", lambda: 0.00005)
    png = BytesIO()
    Image.new("RGB", (1, 1), "white").save(png, format="PNG")
    payload = {
        "model": "poisoned-upstream-alias",
        "created": 1,
        "data": [
            {
                "b64_json": base64.b64encode(png.getvalue()).decode(),
                "media_type": "image/png",
            }
        ],
        "usage": {"cost": 0.007},
    }
    transport = AsyncMock(
        side_effect=asyncio.CancelledError()
        if failed == "cancelled"
        else TimeoutError()
        if failed
        else None,
        return_value=payload,
    )
    monkeypatch.setattr(images, "generate_buffered_image", transport)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/images",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer sk-test"),
            ],
        }
    )

    async def dispatch() -> Any:
        if via_route:
            return await integration_client.post(
                "/v1/images",
                json={"model": "image-model", "prompt": "test"},
                headers={"authorization": "Bearer sk-test"},
            )
        return await images.forward_image_request(
            request, session, b'{"model":"image-model","prompt":"test"}'
        )

    if failed == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await dispatch()
    else:
        response = await dispatch()
        assert response.status_code == (502 if failed else 200)
    transport.assert_awaited_once()
    await session.refresh(key)
    assert key.balance == (1000000 if failed else 846000)
    assert key.reserved_balance == 0
    releases = (
        await session.exec(
            select(ReservationRelease).where(
                ReservationRelease.billing_key_hash == key.hashed_key
            )
        )
    ).all()
    assert len(releases) == 1
    if not failed:
        body = response.json() if via_route else json.loads(response.body)
        assert body["model"] == "image-model"
        assert body["usage"]["cost"]["total_msats"] == 154000
        assert body["usage"].get("prompt_tokens", 0) == 0
