"""Regression coverage for certification pricing and probe lifecycle fixes."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from routstr.payment import price
from routstr.payment.cost_calculation import calculate_cost
from routstr.upstream.certification import (
    _model_from_usd_pricing,
    certify_upstream_url,
    cost_prompt_completion_row,
    probe_upstream,
    run_live_checks,
)
from routstr.upstream.certification_cache import (
    CacheProbeResult,
    cache_reported_row,
    cost_margin_row,
    probe_cache,
)


@pytest.mark.asyncio
async def test_byok_cost_and_margin_include_inference_and_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price, "SATS_USD_PRICE", 0.001)
    model = _model_from_usd_pricing("test-model", 1e-6, 2e-6, 0.001)
    payload = {
        "model": "test-model",
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "is_byok": True,
            "cost": 0.00005,
            "cost_details": {"upstream_inference_cost": 0.001},
        },
    }
    from routstr.upstream.certification import ProbeResult

    probe = ProbeResult(
        base_url="https://mock.example/v1",
        models_url="https://mock.example/v1/models",
        chat_url="https://mock.example/v1/chat/completions",
        chat_status=200,
        chat_payload=payload,
    )
    cost = await calculate_cost(payload, 1_000_000, model_obj=model, provider_fee=1)
    row = cost_prompt_completion_row(
        model=model, probe=probe, cost_data=cost, provider_fee=1, sats_to_usd=0.001
    )
    assert row["status"] == "ok"
    assert row["evidence"]["expected_total_msats"] == 1050
    margin = cost_margin_row(
        model=model, payloads=[payload], provider_fee=1, sats_to_usd=0.001
    )
    assert margin["status"] == "fail"
    assert margin["evidence"]["samples"][0]["upstream_msats_with_fee"] == 1050


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage", "entry", "fee", "explicit", "expected"),
    [
        (
            {"prompt_tokens": 1000, "completion_tokens": 1},
            {"input_cost_per_token": 9e-6, "output_cost_per_token": 9e-6},
            2,
            True,
            2004,
        ),
        (
            {
                "prompt_tokens": 1000,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 900},
            },
            {
                "cache_read_input_token_cost": -1,
                "cache_creation_input_token_cost": float("inf"),
            },
            1,
            False,
            1002,
        ),
        (
            {
                "prompt_tokens": 1000,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 900},
            },
            {"cache_read_input_token_cost": 1e-7},
            1,
            False,
            192,
        ),
        (
            {
                "input_tokens": 100,
                "output_tokens": 1,
                "cache_creation_input_tokens": 900,
            },
            {"cache_creation_input_token_cost": 1.25e-6},
            2,
            False,
            2454,
        ),
        (
            {"prompt_tokens": 1000, "completion_tokens": 1, "cost": 0.001002},
            {},
            2,
            True,
            2004,
        ),
    ],
)
async def test_standalone_preserves_fee_cache_rates_and_usd_fee(
    monkeypatch: pytest.MonkeyPatch,
    usage: dict[str, Any],
    entry: dict[str, float],
    fee: float,
    explicit: bool,
    expected: int,
) -> None:
    from routstr.payment import models

    monkeypatch.setattr(price, "SATS_USD_PRICE", None)
    monkeypatch.setattr(price, "BTC_USD_PRICE", None)
    monkeypatch.setattr(
        models,
        "litellm_cost_entry",
        lambda _: {
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
            **entry,
        },
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        return httpx.Response(200, json={"model": "test-model", "usage": usage})

    kwargs: dict[str, Any] = (
        {"prompt_price": 1e-6, "completion_price": 2e-6} if explicit else {}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await certify_upstream_url(
            "https://mock.example/v1",
            model_id="test-model",
            provider_fee=fee,
            sats_usd_price=0.001,
            check_cache=False,
            client=client,
            **kwargs,
        )
    row = next(row for row in result["rows"] if row["id"] == "cost.prompt_completion")
    assert row["status"] == "ok", row
    assert row["evidence"]["actual_total_msats"] == expected


class TrickleBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False
        self.started = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        for chunk in (b'{"data":', b"[]", b"}"):
            await asyncio.sleep(0.03)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["models", "chat", "cache"])
async def test_probe_elapsed_deadline_closes_trickling_response(mode: str) -> None:
    body = TrickleBody()

    def handle(request: httpx.Request) -> httpx.Response:
        if mode == "chat" and request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        if mode == "cache":
            result = await probe_cache(
                "https://mock.example/v1", "", "test-model", client=client, timeout=0.05
            )
            assert result.statuses == [None]
            assert "TimeoutError" in (result.errors[0] or "")
        else:
            probe = await probe_upstream(
                "https://mock.example/v1",
                "",
                "test-model" if mode == "chat" else "",
                client=client,
                timeout=0.05,
            )
            if mode == "chat":
                assert probe.chat_status is None
                assert "TimeoutError" in (probe.chat_error or "")
            else:
                assert probe.models_status is None
                assert "TimeoutError" in (probe.models_error or "")
        assert body.closed
        assert not client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", [False, True])
@pytest.mark.parametrize("owns_client", [False, True])
async def test_probe_cancellation_closes_body_and_owned_client(
    monkeypatch: pytest.MonkeyPatch, cache: bool, owns_client: bool
) -> None:
    body = TrickleBody()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
    )
    if owns_client:
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_: client)
    probe = probe_cache if cache else probe_upstream
    task = asyncio.create_task(
        probe("https://mock.example/v1", "", "", client=None if owns_client else client)
    )
    await body.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed
    assert client.is_closed == owns_client
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_failed_initial_completion_skips_cache(status: int) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        calls.append(request)
        return httpx.Response(status, json={"error": "failed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        rows = await run_live_checks(
            "https://mock.example/v1",
            "",
            _model_from_usd_pricing("test-model", 1e-6, 2e-6, 0.001),
            provider_fee=1,
            sats_to_usd=0.001,
            client=client,
        )
    assert len(calls) == 1
    assert (
        next(row for row in rows if row["id"] == "cache.reported")["status"] == "warn"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_cache_does_not_retry_non_format_errors(status: int) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error": "failed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await probe_cache(
            "https://mock.example/v1", "", "test-model", client=client
        )
    assert len(calls) == 1
    assert result.statuses == [status]


@pytest.mark.parametrize(
    "usage",
    [
        {"cache_creation_input_tokens": 3000},
        {"prompt_tokens_details": {"cache_creation_tokens": 3000}},
        {"prompt_tokens_details": {"cache_write_tokens": 3000}},
        {"input_tokens_details": {"cache_write_tokens": 3000}},
    ],
)
@pytest.mark.parametrize("unknown", [False, True])
def test_known_cache_writes_are_no_hit_not_unrecognized(
    usage: dict[str, Any], unknown: bool
) -> None:
    usage = {"input_tokens": 10, "output_tokens": 1, **usage}
    if unknown:
        usage["unknown_cached_read_tokens"] = 5
    payload = {"usage": usage}
    row = cache_reported_row(
        CacheProbeResult(
            chat_url="mock", statuses=[200, 200], payloads=[payload, payload]
        )
    )
    assert row["status"] == ("fail" if unknown else "warn")
    assert row["evidence"]["second_usage"]["cache_write_tokens"] == 3000
    assert row["evidence"].get("unrecognised_cache_fields", []) == (
        ["unknown_cached_read_tokens"] if unknown else []
    )
