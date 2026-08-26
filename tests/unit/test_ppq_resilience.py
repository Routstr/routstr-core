from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from routstr.upstream.ppqai import (
    PPQAIUpstreamProvider,
    PPQCircuitOpenError,
    _ppq_circuits,
    _safe_read_request,
)


@pytest.fixture(autouse=True)
def _clear_ppq_circuits() -> None:
    _ppq_circuits.clear()


@pytest.mark.asyncio
async def test_safe_ppq_read_retries_timeout_with_bounded_backoff() -> None:
    request = httpx.Request("GET", "https://api.ppq.ai/models")
    success = httpx.Response(200, request=request, json={"data": []})
    client = MagicMock()
    client.request = AsyncMock(
        side_effect=[httpx.ReadTimeout("", request=request), success]
    )
    with (
        patch("routstr.upstream.ppqai.random.uniform", return_value=0.0),
        patch("routstr.upstream.ppqai.asyncio.sleep", AsyncMock()) as sleep,
    ):
        response = await _safe_read_request(
            client, "GET", "https://api.ppq.ai/models", headers={}
        )

    assert response is success
    assert client.request.await_count == 2
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
async def test_safe_ppq_read_opens_cross_cycle_circuit_and_probe_clears_it() -> None:
    request = httpx.Request("GET", "https://api.ppq.ai/models")
    client = MagicMock()
    client.request = AsyncMock(side_effect=httpx.ReadTimeout("down", request=request))

    with (
        patch("routstr.upstream.ppqai.random.uniform", return_value=0.0),
        patch("routstr.upstream.ppqai.asyncio.sleep", AsyncMock()),
        patch("routstr.upstream.ppqai.time.monotonic", return_value=100.0),
        pytest.raises(httpx.ReadTimeout),
    ):
        await _safe_read_request(client, "GET", str(request.url), headers={})
    assert client.request.await_count == 3

    with (
        patch("routstr.upstream.ppqai.time.monotonic", return_value=110.0),
        pytest.raises(PPQCircuitOpenError),
    ):
        await _safe_read_request(client, "GET", str(request.url), headers={})
    assert client.request.await_count == 3

    success = httpx.Response(200, request=request, json={"data": []})
    client.request = AsyncMock(return_value=success)
    with patch("routstr.upstream.ppqai.time.monotonic", return_value=131.0):
        assert (
            await _safe_read_request(client, "GET", str(request.url), headers={})
            is success
        )

    state = next(iter(_ppq_circuits.values()))
    assert state.consecutive_failures == 0
    assert state.cooldown_until == 0.0


@pytest.mark.asyncio
async def test_fetch_models_failure_is_not_a_valid_empty_catalog() -> None:
    provider = PPQAIUpstreamProvider("secret")
    with (
        patch(
            "routstr.upstream.ppqai._safe_read_request",
            AsyncMock(side_effect=httpx.ReadTimeout("catalog timed out")),
        ),
        pytest.raises(httpx.ReadTimeout),
    ):
        await provider.fetch_models()


@pytest.mark.asyncio
async def test_ppq_invoice_creation_post_is_never_retried() -> None:
    provider = PPQAIUpstreamProvider("secret")
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ReadTimeout("invoice timed out"))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("routstr.upstream.ppqai.httpx.AsyncClient", return_value=context),
        pytest.raises(httpx.ReadTimeout),
    ):
        await provider.create_lightning_topup(10, "USD")

    client.post.assert_awaited_once()
