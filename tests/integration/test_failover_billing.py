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
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ReservationRelease
from routstr.payment.models import Architecture, Model, Pricing
from routstr.proxy import refresh_model_maps
from routstr.upstream.base import BaseUpstreamProvider

CHEAP_BASE_URL = "https://cheap.example.com/v1"
EXPENSIVE_BASE_URL = "https://expensive.example.com/v1"
THIRD_BASE_URL = "https://third.example.com/v1"


@pytest.fixture(autouse=True)
def _no_upstream_5xx_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the same-upstream retry backoff out of the test runtime."""
    monkeypatch.setattr("routstr.proxy._UPSTREAM_5XX_RETRY_BACKOFF_SECONDS", 0)


def _make_model(
    model_id: str,
    prompt_sats: float,
    completion_sats: float,
    max_cost: float = 50.0,
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
            prompt=prompt_sats, completion=completion_sats, max_cost=max_cost
        ),
        sats_pricing=Pricing(
            prompt=prompt_sats, completion=completion_sats, max_cost=max_cost
        ),
    )


class _StaticProvider(BaseUpstreamProvider):
    """Upstream provider with a fixed model catalog and no remote refresh."""

    def __init__(self, base_url: str, api_key: str, fee: float, model: Model) -> None:
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
        _make_model("provb/dual-model", 0.005, 0.010, max_cost=100.0),
    )
    async for _ in _install_providers([cheap, expensive]):
        yield cheap, expensive


def _successful_upstream_response() -> httpx.Response:
    """Return a successful completion with deterministic token usage."""
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


def _upstream_response(request: httpx.Request) -> httpx.Response:
    """502 from the cheap (winning) provider; a served completion elsewhere."""
    if request.url.host == "cheap.example.com":
        return httpx.Response(
            502,
            content=json.dumps({"error": {"message": "bad gateway"}}).encode(),
            headers={"content-type": "application/json"},
        )
    return _successful_upstream_response()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failover_serve_billed_at_serving_providers_rate(
    authenticated_client: AsyncClient,
    dual_provider_maps: tuple[_StaticProvider, _StaticProvider],
    integration_session: AsyncSession,
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

    # The winner is tried twice: its 502 is retried in place before failover.
    assert [r.url.host for r in sent_requests] == [
        "cheap.example.com",
        "cheap.example.com",
        "expensive.example.com",
    ]

    # The fallback must be asked for ITS OWN model spelling, not the winner's.
    forwarded_body = json.loads(sent_requests[2].content)
    assert forwarded_body["model"] == "provb/dual-model"

    # The response echo names the model that actually served.
    assert payload["model"] == "provb/dual-model"

    # Billed at the serving provider's rate: 1000/1000*5000 + 500/1000*10000.
    assert payload["cost"]["total_msats"] == 10_000

    # The fallback's larger max-cost envelope requires a replacement
    # reservation. The failed candidate is released, the serving candidate is
    # charged, and no request-owned reservation remains active.
    key_hash = authenticated_client._test_api_key.removeprefix("sk-")  # type: ignore[attr-defined]
    records = (
        await integration_session.exec(
            select(ReservationRelease).where(ReservationRelease.key_hash == key_hash)
        )
    ).all()
    assert sorted(record.status for record in records) == ["charged", "released"]


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
async def test_same_id_cheapest_provider_serves_and_bills_its_own_price(
    authenticated_client: AsyncClient,
    same_id_provider_maps: None,
) -> None:
    """Exact same-ID candidates route cheapest and bill that provider's model."""
    sent_requests: list[httpx.Request] = []

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        sent_requests.append(request)
        return _successful_upstream_response()

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
    assert [request.url.host for request in sent_requests] == ["cheap.example.com"]
    assert response.json()["cost"]["total_msats"] == 2_000


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
    # The winner's 502 is retried in place before failover.
    assert [r.url.host for r in sent_requests] == [
        "cheap.example.com",
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
                content=json.dumps({"error": {"message": "bad gateway"}}).encode(),
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
    # The winner's 502 is retried in place before failover.
    assert [r.url.host for r in sent_requests] == [
        "cheap.example.com",
        "cheap.example.com",
        "expensive.example.com",
    ]
    assert response.json()["cost"]["total_msats"] == 3_000


@pytest.fixture
async def envelope_split_provider_maps(
    patched_db_engine: None,
) -> AsyncGenerator[None, None]:
    """Same-id providers where the fallback's max cost dwarfs the key balance."""
    cheap = _StaticProvider(
        CHEAP_BASE_URL,
        "key-cheap",
        1.0,
        _make_model("dual-model", 0.001, 0.002, max_cost=50.0),
    )
    expensive = _StaticProvider(
        EXPENSIVE_BASE_URL,
        "key-expensive",
        1.0,
        _make_model("dual-model", 0.005, 0.010, max_cost=20_000.0),
    )
    async for _ in _install_providers([cheap, expensive]):
        yield


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failover_beyond_balance_envelope_is_rejected(
    authenticated_client: AsyncClient,
    envelope_split_provider_maps: None,
) -> None:
    """A fallback whose max-cost envelope exceeds the balance is not served.

    Admission and reservation are sized to the best-ranked candidate's max
    cost. When that candidate fails and the next one's envelope exceeds the
    key's balance, serving it could settle far beyond what admission allowed,
    so the request must be rejected (as it would be if the pricier candidate
    were ranked first) instead of forwarded.
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

    # The 20_000-sat envelope exceeds the key's 10_000-sat balance: the
    # fallback must be rejected before its upstream is ever contacted.
    assert response.status_code == 402
    # The winner is retried in place; the fallback is still never contacted.
    assert [r.url.host for r in sent_requests] == [
        "cheap.example.com",
        "cheap.example.com",
    ]
@pytest.fixture
async def raised_envelope_provider_maps(
    patched_db_engine: None,
) -> AsyncGenerator[None, None]:
    """Same-id providers where the fallback needs a larger, affordable reserve."""
    cheap = _StaticProvider(
        CHEAP_BASE_URL,
        "key-cheap",
        1.0,
        _make_model("dual-model", 0.001, 0.002, max_cost=50.0),
    )
    expensive = _StaticProvider(
        EXPENSIVE_BASE_URL,
        "key-expensive",
        1.0,
        _make_model("dual-model", 0.005, 0.010, max_cost=100.0),
    )
    async for _ in _install_providers([cheap, expensive]):
        yield


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failover_reserves_serving_candidates_envelope(
    authenticated_client: AsyncClient,
    raised_envelope_provider_maps: None,
    integration_session: AsyncSession,
) -> None:
    """An affordable pricier fallback is re-reserved, served, and billed.

    The fallback's max cost (100 sats) exceeds the winner's (50 sats) but fits
    the key's balance, so the reservation is raised to the serving candidate's
    envelope and the request completes, billed at the serving rate with the
    unused reserve refunded.
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
    # The winner's 502 is retried in place before failover.
    assert [r.url.host for r in sent_requests] == [
        "cheap.example.com",
        "cheap.example.com",
        "expensive.example.com",
    ]
    assert response.json()["cost"]["total_msats"] == 10_000

    key_hash = authenticated_client._test_api_key.removeprefix("sk-")  # type: ignore[attr-defined]
    records = (
        await integration_session.exec(
            select(ReservationRelease).where(ReservationRelease.key_hash == key_hash)
        )
    ).all()
    assert len(records) == 2
    released = next(record for record in records if record.status == "released")
    charged = next(record for record in records if record.status == "charged")
    assert charged.reserved_msats > released.reserved_msats
    assert all(record.status != "active" for record in records)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transient_502_retries_same_upstream_before_failing_over(
    authenticated_client: AsyncClient,
    dual_provider_maps: tuple[_StaticProvider, _StaticProvider],
) -> None:
    """A transient 502 is retried on the same upstream, not failed over at once.

    The winner answers the first attempt with a 502 and the second with a
    completion, so the pricier fallback is never contacted and the request is
    billed at the winner's rate (2_000 msats, not the fallback's 10_000).
    """
    sent_requests: list[httpx.Request] = []
    cheap_attempts = 0

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        nonlocal cheap_attempts
        sent_requests.append(request)
        if request.url.host == "cheap.example.com":
            cheap_attempts += 1
            if cheap_attempts == 1:
                return httpx.Response(
                    502,
                    content=json.dumps({"error": {"message": "bad gateway"}}).encode(),
                    headers={"content-type": "application/json"},
                )
        return _successful_upstream_response()

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
    # Retried in place: same host twice, fallback never consulted.
    assert [r.url.host for r in sent_requests] == [
        "cheap.example.com",
        "cheap.example.com",
    ]
    payload = response.json()
    assert payload["model"] == "prova/dual-model"
    # Billed at the winner's rate, not the fallback's 10_000.
    assert payload["cost"]["total_msats"] == 2_000


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transport_failure_is_not_retried_in_place(
    authenticated_client: AsyncClient,
    dual_provider_maps: tuple[_StaticProvider, _StaticProvider],
) -> None:
    """A transport failure fails over at once instead of retrying in place.

    The proxy maps a connect/timeout error to a 502 of its own, so the upstream
    may already have accepted and billed the request: re-sending it is not safe.
    """
    sent_requests: list[httpx.Request] = []

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        sent_requests.append(request)
        if request.url.host == "cheap.example.com":
            raise httpx.ConnectError("connection refused", request=request)
        return _successful_upstream_response()

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unsupported_param_502_is_corrected_on_same_upstream(
    authenticated_client: AsyncClient,
    dual_provider_maps: tuple[_StaticProvider, _StaticProvider],
) -> None:
    """A 502 that names an unsupported param retries without it, in place.

    Some upstreams report a rejected param as a 502 rather than a 400. Blindly
    re-sending the same body would fail again, so the param is stripped first.
    """
    sent_bodies: list[tuple[str, dict[str, Any]]] = []

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        body = json.loads(await request.aread())
        sent_bodies.append((request.url.host, body))
        if "temperature" in body:
            return httpx.Response(
                502,
                content=json.dumps(
                    {
                        "error": {
                            "message": "Codex error: Unsupported parameter: temperature"
                        }
                    }
                ).encode(),
                headers={"content-type": "application/json"},
            )
        return _successful_upstream_response()

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
                "temperature": 0.2,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert [host for host, _ in sent_bodies] == [
        "cheap.example.com",
        "cheap.example.com",
    ]
    assert "temperature" in sent_bodies[0][1]
    assert "temperature" not in sent_bodies[1][1]
    assert response.json()["cost"]["total_msats"] == 2_000


def _successful_stream_response() -> httpx.Response:
    """Return a served chat completion stream with deterministic usage."""
    chunks = [
        {
            "id": "chatcmpl-served",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "dual-model",
            "choices": [{"index": 0, "delta": {"content": "hi"}}],
        },
        {
            "id": "chatcmpl-served",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "dual-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
            },
        },
    ]
    content = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    return httpx.Response(
        200,
        content=content + b"data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _successful_responses_api_response() -> httpx.Response:
    """Return a served Responses API result with deterministic usage."""
    body = {
        "id": "resp_served",
        "object": "response",
        "created_at": 1,
        "model": "dual-model",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}],
            }
        ],
        "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
    }
    return httpx.Response(
        200,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )


_UNSUPPORTED_TEMPERATURE = "Codex error: Unsupported parameter: temperature"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 500, 502])
@pytest.mark.parametrize(
    "error_body",
    [
        {"error": {"message": _UNSUPPORTED_TEMPERATURE}},
        {"error": _UNSUPPORTED_TEMPERATURE},
        {"message": _UNSUPPORTED_TEMPERATURE},
        {"detail": _UNSUPPORTED_TEMPERATURE},
    ],
    ids=["error.message", "error-string", "message", "detail"],
)
@pytest.mark.parametrize("endpoint", ["chat", "chat-stream", "responses"])
async def test_unsupported_param_is_corrected_on_every_path(
    authenticated_client: AsyncClient,
    dual_provider_maps: tuple[_StaticProvider, _StaticProvider],
    status_code: int,
    error_body: dict[str, Any],
    endpoint: str,
) -> None:
    """Chat, streaming chat and Responses requests all strip the named param.

    The rejection is corrected in place whether the upstream answers 400 or
    5xx and whatever JSON shape it wraps the message in.
    """
    sent_bodies: list[tuple[str, dict[str, Any]]] = []

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        body = json.loads(await request.aread())
        sent_bodies.append((request.url.host, body))
        if "temperature" in body:
            return httpx.Response(
                status_code,
                content=json.dumps(error_body).encode(),
                headers={"content-type": "application/json"},
            )
        if endpoint == "chat-stream":
            return _successful_stream_response()
        if endpoint == "responses":
            return _successful_responses_api_response()
        return _successful_upstream_response()

    if endpoint == "responses":
        url = "/v1/responses"
        payload: dict[str, Any] = {
            "model": "dual-model",
            "temperature": 0.2,
            "input": "hello",
        }
    else:
        url = "/v1/chat/completions"
        payload = {
            "model": "dual-model",
            "temperature": 0.2,
            "messages": [{"role": "user", "content": "hello"}],
        }
        if endpoint == "chat-stream":
            payload["stream"] = True

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
        response = await authenticated_client.post(url, json=payload)
        content = await response.aread()

    assert response.status_code == 200, content
    assert [host for host, _ in sent_bodies] == [
        "cheap.example.com",
        "cheap.example.com",
    ]
    assert "temperature" in sent_bodies[0][1]
    assert sent_bodies[1][1] == {
        k: v for k, v in sent_bodies[0][1].items() if k != "temperature"
    }
    assert b"hi" in content


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 502])
@pytest.mark.parametrize(
    ("param", "message"),
    [
        (
            "model",
            "Codex error: The 'gpt-6-luna' model is not supported when using "
            "Codex with a ChatGPT account.",
        ),
        ("model", "Unsupported parameter: model"),
        ("stream", "stream is not supported on this endpoint"),
        ("messages", "Unsupported parameter: messages"),
    ],
)
async def test_request_identity_rejection_fails_over_with_body_intact(
    authenticated_client: AsyncClient,
    dual_provider_maps: tuple[_StaticProvider, _StaticProvider],
    status_code: int,
    param: str,
    message: str,
) -> None:
    """A rejection naming the request itself never strips it.

    Dropping ``model`` would carry a model-less body into failover, where an
    upstream may serve its default model instead of the one paid for.
    """
    sent_bodies: list[tuple[str, dict[str, Any]]] = []

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        body = json.loads(await request.aread())
        sent_bodies.append((request.url.host, body))
        if request.url.host == "cheap.example.com":
            return httpx.Response(
                status_code,
                content=json.dumps({"error": {"message": message}}).encode(),
                headers={"content-type": "application/json"},
            )
        return _successful_upstream_response()

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
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert all(param in body for _, body in sent_bodies)
    assert sent_bodies[-1][0] == "expensive.example.com"
    assert sent_bodies[-1][1]["model"] == "provb/dual-model"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 502])
@pytest.mark.parametrize(
    "message",
    [
        "Codex error: Unsupported parameter: tools[0].strict",
        "Unsupported parameter: 'tools[0].strict'",
        "Unsupported parameter: reasoning.summary",
    ],
)
async def test_nested_param_rejection_keeps_parent_param(
    authenticated_client: AsyncClient,
    dual_provider_maps: tuple[_StaticProvider, _StaticProvider],
    status_code: int,
    message: str,
) -> None:
    """A nested path in the error never strips its whole top-level parent."""
    sent_bodies: list[tuple[str, dict[str, Any]]] = []
    tools = [
        {
            "type": "function",
            "function": {"name": "f", "parameters": {}, "strict": True},
        }
    ]

    async def fake_transport(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        body = json.loads(await request.aread())
        sent_bodies.append((request.url.host, body))
        if request.url.host == "cheap.example.com":
            return httpx.Response(
                status_code,
                content=json.dumps({"error": {"message": message}}).encode(),
                headers={"content-type": "application/json"},
            )
        return _successful_upstream_response()

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
                "tools": tools,
                "reasoning": {"effort": "low"},
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert all(
        body["tools"] == tools and body["reasoning"] == {"effort": "low"}
        for _, body in sent_bodies
    )
    assert sent_bodies[-1][0] == "expensive.example.com"
