"""An image generation is a purchase: one candidate, one POST, no re-send.

Chat requests fail over to the next provider and re-send on a gateway 5xx.
A generation cannot: an ambiguous failure after dispatch does not prove the
upstream did not charge for it, so trying another provider can buy the
image twice while only one response is settled.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr import proxy as proxy_module
from routstr.auth import ReservationSnapshot
from routstr.core.db import ApiKey
from routstr.core.exceptions import UpstreamError
from routstr.core.settings import settings
from routstr.payment.image_pricing import ImagePriceTier, ImagePricing
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.openrouter import OpenRouterUpstreamProvider

BOOK = ImagePricing(max_usd=0.04, tiers=[ImagePriceTier(usd=0.04)])


def _model(output_modalities: list[str]) -> Model:
    return Model(
        id="test-model",
        name="test-model",
        created=0,
        description="",
        context_length=0,
        architecture=Architecture(
            modality="text->image",
            input_modalities=["text"],
            output_modalities=output_modalities,
            tokenizer="Unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.0, completion=0.0, image_output=0.04),
        sats_pricing=Pricing(prompt=0.0, completion=0.0, image_output=40.0),
        image_pricing=BOOK if output_modalities == ["image"] else None,
    )


IMAGE_MODEL = _model(["image"])
CHAT_MODEL = _model(["text"])


def _request(body: dict) -> MagicMock:
    request = MagicMock()
    request.method = "POST"
    request.headers = {"authorization": "Bearer sk-key"}
    request.body = AsyncMock(return_value=json.dumps(body).encode())
    request.state = MagicMock()
    request.state.request_id = "req-1"
    return request


def _upstream(base_url: str, forward: AsyncMock) -> MagicMock:
    upstream = MagicMock()
    upstream.provider_type = "test"
    upstream.base_url = base_url
    upstream.db_id = None
    upstream.prepare_headers = MagicMock(side_effect=lambda h: h)
    upstream.forward_request = forward
    return upstream


async def _run_proxy(
    path: str, candidates: list[tuple[Model, MagicMock]], revert: AsyncMock
) -> Any:
    key = ApiKey(hashed_key="imagekey", balance=10_000_000)
    reservation = ReservationSnapshot(
        release_id="release",
        key_hash=key.hashed_key,
        billing_key_hash=key.hashed_key,
        reserved_msats=40_000,
    )
    with (
        patch.object(proxy_module, "get_candidates", return_value=candidates),
        patch.object(
            proxy_module, "get_max_cost_for_model", AsyncMock(return_value=40_000)
        ),
        patch.object(
            proxy_module,
            "calculate_discounted_max_cost",
            AsyncMock(return_value=40_000),
        ),
        patch.object(proxy_module, "check_token_balance", MagicMock()),
        patch.object(proxy_module, "get_bearer_token_key", AsyncMock(return_value=key)),
        patch.object(
            proxy_module, "pay_for_request", AsyncMock(return_value=reservation)
        ),
        patch.object(proxy_module, "revert_pay_for_request", revert),
        patch.object(proxy_module.asyncio, "sleep", AsyncMock()),
    ):
        request = _request({"model": "test-model", "prompt": "a cat"})
        return await proxy_module._proxy(
            request, path, MagicMock(), await request.body()
        )


def _gateway_502() -> UpstreamError:
    return UpstreamError("bad gateway", status_code=502, from_upstream_response=True)


@pytest.mark.asyncio
async def test_image_generation_does_not_fail_over_to_a_second_provider() -> None:
    first = _upstream("https://a.example", AsyncMock(side_effect=_gateway_502()))
    second = _upstream("https://b.example", AsyncMock(return_value=MagicMock()))
    revert = AsyncMock(return_value=True)

    response = await _run_proxy(
        "v1/images/generations",
        [(IMAGE_MODEL, first), (IMAGE_MODEL, second)],
        revert,
    )

    assert response.status_code == 424
    first.forward_request.assert_awaited_once()
    second.forward_request.assert_not_awaited()
    revert.assert_awaited_once()


@pytest.mark.asyncio
async def test_image_generation_is_not_re_sent_on_a_gateway_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "upstream_5xx_retry_attempts", 2)
    only = _upstream("https://a.example", AsyncMock(side_effect=_gateway_502()))

    response = await _run_proxy(
        "v1/images/generations", [(IMAGE_MODEL, only)], AsyncMock(return_value=True)
    )

    assert response.status_code == 424
    only.forward_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_still_fails_over_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the single-dispatch rule is scoped to image routes."""
    monkeypatch.setattr(settings, "upstream_5xx_retry_attempts", 1)
    served = MagicMock(status_code=200)
    first = _upstream("https://a.example", AsyncMock(side_effect=_gateway_502()))
    second = _upstream("https://b.example", AsyncMock(return_value=served))

    response = await _run_proxy(
        "v1/chat/completions",
        [(CHAT_MODEL, first), (CHAT_MODEL, second)],
        AsyncMock(return_value=True),
    )

    assert response is served
    assert first.forward_request.await_count == 2
    second.forward_request.assert_awaited_once()


def test_openrouter_pins_image_generations_to_one_provider() -> None:
    provider = OpenRouterUpstreamProvider(api_key="k")
    body = json.dumps({"model": "test-model", "prompt": "a cat"}).encode()

    out = provider.prepare_request_body(body, IMAGE_MODEL)
    assert out is not None
    assert json.loads(out)["provider"] == {"allow_fallbacks": False}

    body = json.dumps(
        {"model": "test-model", "prompt": "a cat", "provider": {"order": ["X"]}}
    ).encode()
    out = provider.prepare_request_body(body, IMAGE_MODEL)
    assert out is not None
    assert json.loads(out)["provider"] == {"order": ["X"], "allow_fallbacks": False}


def test_openrouter_leaves_chat_routing_alone() -> None:
    provider = OpenRouterUpstreamProvider(api_key="k")
    body = json.dumps({"model": "test-model", "messages": []}).encode()
    out = provider.prepare_request_body(body, CHAT_MODEL)
    assert out is not None
    assert "provider" not in json.loads(out)
