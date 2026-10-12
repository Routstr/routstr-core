"""Integration test: POST /v1/decisions routed to OpenAI and billed from usage."""

import json
from typing import Any, AsyncGenerator
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient

from routstr.payment.models import Architecture, Model, Pricing
from routstr.proxy import refresh_model_maps
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.openai import OpenAIUpstreamProvider

DECISIONS_REQUEST = {
    "model": "gpt-6-luna",
    "state": {"ticket": "Checkout shows a blank page after I click Pay."},
    "questions": {
        "team": {
            "type": "choice",
            "instructions": "Which team should own this ticket?",
            "criteria": {"payments": "Checkout and billing", "account": "Login"},
        }
    },
}

DECISIONS_RESPONSE = {
    "model": "gpt-6-luna",
    "answers": {"team": {"type": "choice", "choice": "payments"}},
    "usage": {"input_tokens": 1000, "output_tokens": 200},
}


def _luna_model(prompt_sats: float) -> Model:
    return Model(
        id="gpt-6-luna",
        name="gpt-6-luna",
        created=1,
        description="GPT-6 Luna",
        context_length=400_000,
        architecture=Architecture(
            modality="text+image->text",
            input_modalities=["text", "image"],
            output_modalities=["text"],
            tokenizer="GPT",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=prompt_sats, completion=0.0, max_cost=50.0),
        sats_pricing=Pricing(prompt=prompt_sats, completion=0.0, max_cost=50.0),
    )


class _StaticOpenAIProvider(OpenAIUpstreamProvider):
    def __init__(self, model: Model) -> None:
        super().__init__("key-openai", 1.0)
        self._static_model = model

    def get_cached_models(self) -> list[Model]:
        return [self._static_model]

    async def refresh_models_cache(self) -> None:
        pass


class _StaticGenericProvider(BaseUpstreamProvider):
    def __init__(self, model: Model) -> None:
        super().__init__("https://generic.test/v1", "key-generic", 1.0)
        self._static_model = model

    def get_cached_models(self) -> list[Model]:
        return [self._static_model]

    async def refresh_models_cache(self) -> None:
        pass


async def _install(
    upstreams: list[BaseUpstreamProvider],
) -> AsyncGenerator[None, None]:
    from routstr import proxy

    original_upstreams = proxy.get_upstreams()
    with patch("routstr.proxy._upstreams", upstreams):
        await refresh_model_maps()
        yield
    with patch("routstr.proxy._upstreams", original_upstreams):
        await refresh_model_maps()


@pytest.fixture
async def luna_on_openai_and_generic(
    patched_db_engine: None,
) -> AsyncGenerator[None, None]:
    async for _ in _install(
        [
            _StaticGenericProvider(_luna_model(0.0005)),
            _StaticOpenAIProvider(_luna_model(0.001)),
        ]
    ):
        yield


@pytest.fixture
async def luna_on_generic_only(
    patched_db_engine: None,
) -> AsyncGenerator[None, None]:
    async for _ in _install([_StaticGenericProvider(_luna_model(0.0005))]):
        yield


@pytest.mark.integration
@pytest.mark.asyncio
async def test_decisions_routed_to_openai_and_billed_from_usage(
    authenticated_client: AsyncClient,
    luna_on_openai_and_generic: None,
) -> None:
    sent_requests: list[httpx.Request] = []

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        sent_requests.append(request)
        return httpx.Response(
            200,
            content=json.dumps(DECISIONS_RESPONSE).encode(),
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
            "/v1/decisions", json=DECISIONS_REQUEST
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["answers"]["team"]["choice"] == "payments"

    assert len(sent_requests) == 1
    assert str(sent_requests[0].url) == "https://api.openai.com/v1/decisions"
    assert (
        json.loads(sent_requests[0].content)["questions"]
        == (DECISIONS_REQUEST["questions"])
    )

    assert payload["cost"]["input_msats"] == 1000
    assert payload["cost"]["output_msats"] == 0
    assert payload["cost"]["total_msats"] == 1000


@pytest.mark.integration
@pytest.mark.asyncio
async def test_decisions_without_capable_provider_is_rejected(
    authenticated_client: AsyncClient,
    luna_on_generic_only: None,
) -> None:
    with patch("httpx.AsyncHTTPTransport.handle_async_request") as transport:
        response = await authenticated_client.post(
            "/v1/decisions", json=DECISIONS_REQUEST
        )

    assert response.status_code == 400, response.text
    assert "Decisions" in response.json()["error"]["message"]
    transport.assert_not_called()
