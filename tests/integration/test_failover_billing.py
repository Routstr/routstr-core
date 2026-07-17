"""Failover requests are billed and forwarded as the provider that served them.

Covers the whole-system settlement path when two enabled providers expose the
same model under different spellings and prices: the routing winner fails with
a 502, the fallback provider serves, and the response must be billed at the
fallback's configured rate, carry the fallback's model id in the forwarded
request body, and echo the fallback's model id to the client.
"""

import json
from typing import Any, AsyncGenerator
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient

from routstr.payment.models import Architecture, Model, Pricing
from routstr.proxy import refresh_model_maps
from routstr.upstream.base import BaseUpstreamProvider

CHEAP_BASE_URL = "https://cheap.example.com/v1"
EXPENSIVE_BASE_URL = "https://expensive.example.com/v1"


def _make_model(
    model_id: str, prompt_sats: float, completion_sats: float
) -> Model:
    """Build a model whose USD and sats pricing rank consistently."""
    return Model(
        id=model_id,
        name=model_id,
        created=1,
        description="test model",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="gpt",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=prompt_sats, completion=completion_sats, max_cost=50.0
        ),
        sats_pricing=Pricing(
            prompt=prompt_sats, completion=completion_sats, max_cost=50.0
        ),
    )


class _StaticProvider(BaseUpstreamProvider):
    """Upstream provider with a fixed model catalog and no remote refresh."""

    def __init__(
        self, base_url: str, api_key: str, fee: float, model: Model
    ) -> None:
        super().__init__(base_url, api_key, fee)
        self.provider_type = "custom"
        self._static_model = model

    def get_cached_models(self) -> list[Model]:
        return [self._static_model]

    async def refresh_models_cache(self) -> None:
        pass


async def _install_providers(
    providers: list[_StaticProvider],
) -> AsyncGenerator[None, None]:
    """Install providers into the routing maps, restoring the originals after."""
    from routstr import proxy

    original_upstreams = proxy.get_upstreams()
    with patch("routstr.proxy._upstreams", providers):
        await refresh_model_maps()
        yield
    with patch("routstr.proxy._upstreams", original_upstreams):
        await refresh_model_maps()


@pytest.fixture
async def dual_provider_maps(
    patched_db_engine: None,
) -> AsyncGenerator[tuple[_StaticProvider, _StaticProvider], None]:
    """Two same-tail providers under different spellings and prices."""
    cheap = _StaticProvider(
        CHEAP_BASE_URL,
        "key-cheap",
        1.0,
        _make_model("prova/dual-model", 0.001, 0.002),
    )
    expensive = _StaticProvider(
        EXPENSIVE_BASE_URL,
        "key-expensive",
        1.0,
        _make_model("provb/dual-model", 0.005, 0.010),
    )
    async for _ in _install_providers([cheap, expensive]):
        yield cheap, expensive


def _upstream_response(request: httpx.Request) -> httpx.Response:
    """502 from the cheap (winning) provider; a served completion elsewhere."""
    if request.url.host == "cheap.example.com":
        return httpx.Response(
            502,
            content=json.dumps({"error": {"message": "bad gateway"}}).encode(),
            headers={"content-type": "application/json"},
        )
    body = {
        "id": "chatcmpl-served",
        "object": "chat.completion",
        "created": 1,
        "model": "dual-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
        },
    }
    return httpx.Response(
        200,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failover_serve_billed_at_serving_providers_rate(
    authenticated_client: AsyncClient,
    dual_provider_maps: tuple[_StaticProvider, _StaticProvider],
) -> None:
    """A fallback serve is billed at the fallback's price, not the winner's.

    The cheap provider ranks first for the shared tail; it 502s and the
    expensive provider serves 1000 input + 500 output tokens. At the serving
    provider's sats pricing (0.005/0.010 sats per token) that is 10_000 msats;
    at the winner's (0.001/0.002) it would be 2_000 msats.
    """
    sent_requests: list[httpx.Request] = []

    # Patch the network transport (not AsyncClient.send) so the in-process
    # ASGI test client is untouched and only the proxy's upstream hop is mocked.
    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        sent_requests.append(request)
        return _upstream_response(request)

    with (
        patch(
            "httpx.AsyncHTTPTransport.handle_async_request",
            side_effect=fake_transport,
        ),
        # cost_calculation binds sats_usd_price at import time, so the price
        # patch in the app fixture does not reach it; patch its own binding.
        patch(
            "routstr.payment.cost_calculation.sats_usd_price",
            return_value=0.0005,
        ),
    ):
        response = await authenticated_client.post(
            "/v1/chat/completions",
            json={
                "model": "dual-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    payload = response.json()

    # Both providers were attempted, cheapest first.
    assert [r.url.host for r in sent_requests] == [
        "cheap.example.com",
        "expensive.example.com",
    ]

    # The fallback must be asked for ITS OWN model spelling, not the winner's.
    forwarded_body = json.loads(sent_requests[1].content)
    assert forwarded_body["model"] == "provb/dual-model"

    # The response echo names the model that actually served.
    assert payload["model"] == "provb/dual-model"

    # Billed at the serving provider's rate: 1000/1000*5000 + 500/1000*10000.
    assert payload["cost"]["total_msats"] == 10_000


@pytest.fixture
async def same_id_provider_maps(
    patched_db_engine: None,
) -> AsyncGenerator[None, None]:
    """Two providers exposing the IDENTICAL model id at different prices."""
    cheap = _StaticProvider(
        CHEAP_BASE_URL,
        "key-cheap",
        1.0,
        _make_model("dual-model", 0.001, 0.002),
    )
    expensive = _StaticProvider(
        EXPENSIVE_BASE_URL,
        "key-expensive",
        1.0,
        _make_model("dual-model", 0.005, 0.010),
    )
    async for _ in _install_providers([cheap, expensive]):
        yield


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_id_failover_settles_at_serving_price(
    authenticated_client: AsyncClient,
    same_id_provider_maps: None,
) -> None:
    """Settlement must not re-derive pricing from the response's model string.

    Both providers expose the exact same model id, so the forwarded body is
    identical either way — the only observable difference is the settled
    amount. The response's model string resolves to the alias winner (cheap),
    but the expensive provider served, so the bill must be 10_000 msats, not
    the winner's 2_000.
    """
    sent_requests: list[httpx.Request] = []

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        sent_requests.append(request)
        return _upstream_response(request)

    with (
        patch(
            "httpx.AsyncHTTPTransport.handle_async_request",
            side_effect=fake_transport,
        ),
        patch(
            "routstr.payment.cost_calculation.sats_usd_price",
            return_value=0.0005,
        ),
    ):
        response = await authenticated_client.post(
            "/v1/chat/completions",
            json={
                "model": "dual-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert [r.url.host for r in sent_requests] == [
        "cheap.example.com",
        "expensive.example.com",
    ]
    assert response.json()["cost"]["total_msats"] == 10_000


@pytest.mark.integration
@pytest.mark.asyncio
async def test_version_suffixed_model_id_routes(
    authenticated_client: AsyncClient,
    same_id_provider_maps: None,
) -> None:
    """A version-suffixed request (``…-YYYYMMDD``) routes to the base model.

    Model resolution stripped the suffix but the provider lookup did not, so
    such requests resolved a model yet found no provider and 400'd. With the
    unified candidate lookup the strip applies to both.
    """

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        return _upstream_response(request)

    with (
        patch(
            "httpx.AsyncHTTPTransport.handle_async_request",
            side_effect=fake_transport,
        ),
        patch(
            "routstr.payment.cost_calculation.sats_usd_price",
            return_value=0.0005,
        ),
    ):
        response = await authenticated_client.post(
            "/v1/chat/completions",
            json={
                "model": "dual-model-20260101",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200


@pytest.fixture
async def fee_split_provider_maps(
    patched_db_engine: None,
) -> AsyncGenerator[None, None]:
    """Same-tail providers whose fees differ; the serving one charges 1.5x."""
    cheap = _StaticProvider(
        CHEAP_BASE_URL,
        "key-cheap",
        1.0,
        _make_model("dual-model", 0.001, 0.002),
    )
    expensive = _StaticProvider(
        EXPENSIVE_BASE_URL,
        "key-expensive",
        1.5,
        _make_model("dual-model", 0.005, 0.010),
    )
    async for _ in _install_providers([cheap, expensive]):
        yield


@pytest.mark.integration
@pytest.mark.asyncio
async def test_usd_cost_serve_carries_serving_providers_fee(
    authenticated_client: AsyncClient,
    fee_split_provider_maps: None,
) -> None:
    """The USD-cost billing path applies the SERVING provider's fee.

    The upstream that serves reports ``usage.cost`` in USD, so billing goes
    through the USD-cost path where the provider fee is applied explicitly.
    The serving provider's fee is 1.5; the alias winner's is 1.0. At 0.001 USD
    reported cost and 0.0005 USD/sat: 0.001 * 1.5 / 0.0005 = 3 sats = 3000
    msats (fee 1.0 would give 2000).
    """
    sent_requests: list[httpx.Request] = []

    def usd_cost_response(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cheap.example.com":
            return httpx.Response(
                502,
                content=json.dumps(
                    {"error": {"message": "bad gateway"}}
                ).encode(),
                headers={"content-type": "application/json"},
            )
        body = {
            "id": "chatcmpl-usd",
            "object": "chat.completion",
            "created": 1,
            "model": "dual-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost": 0.001,
            },
        }
        return httpx.Response(
            200,
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        sent_requests.append(request)
        return usd_cost_response(request)

    with (
        patch(
            "httpx.AsyncHTTPTransport.handle_async_request",
            side_effect=fake_transport,
        ),
        patch(
            "routstr.payment.cost_calculation.sats_usd_price",
            return_value=0.0005,
        ),
    ):
        response = await authenticated_client.post(
            "/v1/chat/completions",
            json={
                "model": "dual-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert [r.url.host for r in sent_requests] == [
        "cheap.example.com",
        "expensive.example.com",
    ]
    assert response.json()["cost"]["total_msats"] == 3_000
