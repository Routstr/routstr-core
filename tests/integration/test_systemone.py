"""Integration test: POST /v1/systemone proxied and billed end-to-end.

The TypeSafe decision endpoint shares the request plumbing with embeddings:
model id in the top-level ``model`` field, flat ``usage`` in the response.
This test pins the whole path — allowlist, auth, forwarding, and settlement
billed from the response's usage — with only the network hop mocked.
"""

import json
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.payment.models import Architecture, Model, Pricing
from routstr.proxy import refresh_model_maps
from routstr.upstream.base import BaseUpstreamProvider

TYPESAFE_BASE_URL = "https://api.typesafe.ai/v1"

SYSTEMONE_REQUEST = {
    "model": "jev-latest",
    "state": "Help! My payouts have been failing for 3 days.",
    "questions": {
        "is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"}
    },
}

SYSTEMONE_RESPONSE = {
    "model": "jev-latest",
    "answers": {
        "is_urgent": {"type": "noul", "noul": 0.95},
    },
    "usage": {"input_tokens": 1000, "output_tokens": 200},
}


class _StaticTypeSafeProvider(BaseUpstreamProvider):
    """Upstream provider with a fixed TypeSafe model catalog."""

    def __init__(self, base_url: str, api_key: str, fee: float, model: Model) -> None:
        super().__init__(base_url, api_key, fee)
        self.provider_type = "typesafe"
        self._static_model = model

    def get_cached_models(self) -> list[Model]:
        return [self._static_model]

    async def refresh_models_cache(self) -> None:
        pass


def _jev_model(prompt_sats: float = 0.001, completion_sats: float = 0.0) -> Model:
    return Model(
        id="jev-latest",
        name="jev-latest",
        created=1,
        description="TypeSafe System One decision model",
        context_length=64_000,
        architecture=Architecture(
            modality="text->decisions",
            input_modalities=["text"],
            output_modalities=["decisions"],
            tokenizer="Other",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=prompt_sats, completion=completion_sats, max_cost=50.0
        ),
        sats_pricing=Pricing(
            prompt=prompt_sats, completion=completion_sats, max_cost=50.0
        ),
    )


@pytest.fixture
async def typesafe_provider_maps(
    patched_db_engine: None,
) -> AsyncGenerator[_StaticTypeSafeProvider, None]:
    """Install a TypeSafe provider with a priced jev-latest model."""
    provider = _StaticTypeSafeProvider(
        TYPESAFE_BASE_URL,
        "key-typesafe",
        1.0,
        _jev_model(),
    )

    from routstr import proxy

    original_upstreams = proxy.get_upstreams()
    with patch("routstr.proxy._upstreams", [provider]):
        await refresh_model_maps()
        yield provider
    with patch("routstr.proxy._upstreams", original_upstreams):
        await refresh_model_maps()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_systemone_forwarded_and_billed_from_usage(
    authenticated_client: AsyncClient,
    typesafe_provider_maps: _StaticTypeSafeProvider,
    integration_session: AsyncSession,
) -> None:
    """A systemone request reaches api.typesafe.ai and bills input tokens."""
    sent_requests: list[httpx.Request] = []

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        sent_requests.append(request)
        return httpx.Response(
            200,
            content=json.dumps(SYSTEMONE_RESPONSE).encode(),
            headers={"content-type": "application/json"},
        )

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
            "/v1/systemone",
            json=SYSTEMONE_REQUEST,
        )

    assert response.status_code == 200, response.text
    payload = response.json()

    # The answers pass through untouched.
    assert payload["answers"]["is_urgent"]["noul"] == pytest.approx(0.95)

    # Exactly one upstream hop, aimed at TypeSafe's endpoint with the
    # provider's model spelling.
    assert len(sent_requests) == 1
    sent = sent_requests[0]
    assert str(sent.url) == "https://api.typesafe.ai/v1/systemone"
    assert json.loads(sent.content)["model"] == "jev-latest"

    # Billed from usage: 1000 input tokens at 0.001 sats/token = 1000 msats;
    # 200 output tokens at 0 sats = 0. Settled cost is exposed on the body.
    assert payload["cost"]["input_msats"] == 1000
    assert payload["cost"]["output_msats"] == 0
    assert payload["cost"]["total_msats"] == 1000


@pytest.mark.integration
@pytest.mark.asyncio
async def test_systemone_with_x_cashu_settles(
    authenticated_client: AsyncClient,
    typesafe_provider_maps: _StaticTypeSafeProvider,
    testmint_wallet: Any,
) -> None:
    """The X-Cashu settlement gate admits systemone and refunds the delta."""
    token = await testmint_wallet.mint_tokens(10_000)

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(SYSTEMONE_RESPONSE).encode(),
            headers={"content-type": "application/json"},
        )

    # The redemption and refund helpers are bound into base.py at import time,
    # so the app fixture's wallet patches do not reach the proxy's x-cashu path.
    with (
        patch(
            "httpx.AsyncHTTPTransport.handle_async_request",
            side_effect=fake_transport,
        ),
        patch(
            "routstr.upstream.base.recieve_token",
            AsyncMock(return_value=(10_000, "sat", "https://mint.test")),
        ),
        patch(
            "routstr.upstream.base.send_token",
            AsyncMock(return_value="cashuBrefundtoken"),
        ),
        # send_refund derives the mint from the token it just created; the
        # fake token has no mint to parse.
        patch(
            "routstr.upstream.base.token_mint_url",
            return_value="https://mint.test",
        ),
        # cost_calculation binds sats_usd_price at import time, so the price
        # patch in the app fixture does not reach it.
        patch(
            "routstr.payment.cost_calculation.sats_usd_price",
            return_value=0.0005,
        ),
        # Mint trust is node state that other suites mutate; this test is
        # about the endpoint gate, not mint policy.
        patch(
            "routstr.payment.helpers.is_trusted_source_mint",
            return_value=True,
        ),
    ):
        response = await authenticated_client.post(
            "/v1/systemone",
            json=SYSTEMONE_REQUEST,
            headers={"X-Cashu": token},
        )

    # Not rejected as an unsupported endpoint (that returns 400 with
    # x_cashu_unsupported_endpoint), and settled rather than streamed raw.
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["answers"]["is_urgent"]["type"] == "noul"
    # A refund header exists when the token exceeded the settled cost.
    assert response.headers.get("X-Cashu") is not None
