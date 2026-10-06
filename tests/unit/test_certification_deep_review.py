"""Regressions from the independent three-pass certification audit."""

import json
from typing import Any

import httpx
import pytest

from routstr.payment import price
from routstr.payment.cost_calculation import CostData
from routstr.upstream.certification import (
    ProbeResult,
    _model_from_usd_pricing,
    build_checklist,
    cost_prompt_completion_row,
    run_live_checks,
    usage_capture_row,
)
from routstr.upstream.certification_cache import (
    CacheProbeResult,
    cache_billing_row,
    cache_reported_row,
    probe_cache,
)
from routstr.upstream.certification_probe import (
    PROBE_TOKEN_BUDGETS,
    next_probe_budget,
)

SATS_USD = 0.0005
URL = "https://mock.example/v1"
CACHED = {
    "prompt_tokens": 50,
    "completion_tokens": 0,
    "prompt_tokens_details": {"cached_tokens": 45},
    "cost": 5e-5,
}
EXHAUSTED = {
    "error": {
        "message": "Could not finish the message because max_tokens "
        "or model output limit was reached. Please try again with higher max_tokens."
    }
}


def model(name: str = "test-model") -> Any:
    return _model_from_usd_pricing(name, 1e-6, 2e-6, SATS_USD)


def cost(total: int) -> CostData:
    return CostData(base_msats=0, input_msats=total, output_msats=0, total_msats=total)


def cached_probe(usage: dict[str, Any]) -> CacheProbeResult:
    return CacheProbeResult(
        chat_url=f"{URL}/chat/completions",
        statuses=[200, 200],
        payloads=[{"usage": usage}, {"usage": usage}],
    )


def rows_by_id(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {row["id"]: row for row in rows}


@pytest.fixture(autouse=True)
def local_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    from routstr.core.settings import settings

    monkeypatch.setattr(price, "SATS_USD_PRICE", SATS_USD)
    monkeypatch.setattr(settings, "fixed_pricing", False)


@pytest.mark.parametrize("wrong", [0, 1, 999999])
def test_cached_usd_rejects_wrong_calculation(wrong: int) -> None:
    row = cache_billing_row(
        model=model(),
        probe=cached_probe(CACHED),
        cost_data=cost(wrong),
        provider_fee=1.25,
        sats_to_usd=SATS_USD,
    )
    assert row["status"] == "fail"
    assert row["evidence"]["basis"] == "upstream_reported_usd"
    assert row["evidence"]["expected_total_msats"] == 125


def test_cached_usd_verifies_fee_without_claiming_discount() -> None:
    row = cache_billing_row(
        model=model(),
        probe=cached_probe(CACHED),
        cost_data=cost(125),
        provider_fee=1.25,
        sats_to_usd=SATS_USD,
    )
    assert row["status"] == "ok"
    assert row["evidence"]["expected_total_msats"] == 125
    assert row["evidence"]["upstream_discount_verified"] is False
    assert "discount" not in row["title"].lower()
    assert "not verified" in row["detail"]


def test_equal_full_price_does_not_certify_discount() -> None:
    row = cache_billing_row(
        model=model(),
        probe=cached_probe(CACHED),
        cost_data=cost(100),
        sats_to_usd=SATS_USD,
    )
    assert row["status"] == "ok"
    assert (
        row["evidence"]["actual_total_msats"]
        == row["evidence"]["full_price_total_msats"]
    )
    assert row["evidence"]["upstream_discount_verified"] is False
    assert "already carries" not in row["detail"]
    assert "cache rate" not in build_checklist([row])[4]["label"]


@pytest.mark.parametrize(
    "rate,status", [(None, "warn"), (0.0, "fail"), (float("nan"), "fail")]
)
def test_cached_usd_requires_valid_exchange_rate(
    rate: float | None, status: str
) -> None:
    row = cache_billing_row(
        model=model(), probe=cached_probe(CACHED), cost_data=cost(100), sats_to_usd=rate
    )
    assert row["status"] == status


@pytest.mark.parametrize("free", [False, True])
def test_unknown_cost_does_not_claim_upstream_overpayment(free: bool) -> None:
    usage = {key: value for key, value in CACHED.items() if key != "cost"}
    priced = _model_from_usd_pricing("test-model", 0 if free else 1e-6, 0, SATS_USD)
    row = cache_billing_row(
        model=priced, probe=cached_probe(usage), cost_data=cost(0 if free else 100)
    )
    assert row["status"] == "warn"
    assert "pay more" not in row["detail"]
    assert "unverified" in row["detail"]


def test_missing_usage_does_not_claim_free_settlement() -> None:
    probe = ProbeResult(
        base_url=URL,
        models_url=f"{URL}/models",
        chat_url=f"{URL}/chat/completions",
        chat_status=200,
        chat_payload={"choices": [{"message": {"content": "ok"}}]},
    )
    row = usage_capture_row(probe)
    assert row["status"] == "warn"
    assert "estimate" in row["detail"]
    assert "(0+0)" not in row["detail"]


def test_short_usd_basis_is_not_called_configured_pricing() -> None:
    probe = ProbeResult(
        base_url=URL,
        models_url=f"{URL}/models",
        chat_url=f"{URL}/chat/completions",
        chat_status=200,
        chat_payload={"usage": CACHED},
    )
    row = cost_prompt_completion_row(
        model=model(),
        probe=probe,
        cost_data=cost(100),
        provider_fee=1,
        sats_to_usd=SATS_USD,
    )
    assert row["status"] == "ok"
    assert "upstream-reported USD" in row["detail"]
    assert "matching the configured" not in row["detail"]


def test_provider_without_model_rows_keeps_pricing_rows_ok() -> None:
    """Served prices come straight from the catalog when no rows exist."""
    from routstr.core.admin import (
        _report_row_cache_rate,
        _report_row_enabled_models_served,
        _report_row_sats_pricing_present,
        _report_row_served_matches_configured,
    )

    rows = [
        builder([])
        for builder in (
            _report_row_cache_rate,
            _report_row_enabled_models_served,
            _report_row_sats_pricing_present,
            _report_row_served_matches_configured,
        )
    ]
    assert [row["status"] for row in rows] == ["ok"] * 4
    assert (
        next(c for c in build_checklist(rows) if c["goal"] == "pricing_v1_models")[
            "status"
        ]
        == "ok"
    )


@pytest.mark.asyncio
async def test_typesafe_native_listing_without_unsupported_chat() -> None:
    from routstr.upstream.typesafe import TypeSafeUpstreamProvider

    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"models": [{"name": "jev-1.13"}]})

    upstream = TypeSafeUpstreamProvider(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        rows = rows_by_id(
            await run_live_checks(
                upstream.base_url,
                "test-key",
                model("jev-1.13.0"),
                provider_fee=1,
                sats_to_usd=SATS_USD,
                upstream=upstream,
                client=client,
            )
        )
    assert len(calls) == 1
    assert rows["endpoint.models_payload"]["status"] == "ok"
    assert rows["endpoint.models_payload"]["evidence"]["sample_ids"] == ["jev-1.13"]
    for key in (
        "usage.capture",
        "cost.prompt_completion",
        "cache.reported",
        "cache.billing",
        "cost.margin",
    ):
        assert rows[key]["status"] == "warn"
        assert "unverified" in rows[key]["detail"]


@pytest.mark.asyncio
async def test_corrected_field_and_budget_propagate_to_cache() -> None:
    bodies = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        body = json.loads(request.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."
                    }
                },
            )
        if body["max_completion_tokens"] < 128:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "max_completion_tokens must be at least 128 for this deployment."
                    }
                },
            )
        return httpx.Response(200, json={"usage": CACHED})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        rows = rows_by_id(
            await run_live_checks(
                URL, "", model(), provider_fee=1, sats_to_usd=SATS_USD, client=client
            )
        )
    assert rows["usage.capture"]["status"] == "ok"
    assert len(bodies) == 5
    assert [body.get("max_completion_tokens") for body in bodies] == [
        None,
        32,
        128,
        128,
        128,
    ]
    evidence = rows["usage.capture"]["evidence"]
    assert evidence["token_limit_field"] == "max_completion_tokens"
    assert evidence["token_limit"] == 128
    assert [a["status_code"] for a in evidence["attempts"]] == [400, 400, 200]
    assert rows["cache.reported"]["evidence"]["token_limit"] == 128


@pytest.mark.asyncio
async def test_openai_provider_shaped_field_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routstr.upstream.openai import OpenAIUpstreamProvider

    monkeypatch.setattr("routstr.upstream.openai._rejects_max_tokens", lambda _: True)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "gpt-5"}]})
        body = json.loads(request.content)
        assert body["max_completion_tokens"] == 32
        assert "max_tokens" not in body
        return httpx.Response(200, json={"usage": CACHED})

    upstream = OpenAIUpstreamProvider(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        rows = rows_by_id(
            await run_live_checks(
                upstream.base_url,
                "test-key",
                model("gpt-5"),
                provider_fee=1,
                sats_to_usd=SATS_USD,
                upstream=upstream,
                client=client,
            )
        )
    assert (
        rows["usage.capture"]["evidence"]["token_limit_field"]
        == "max_completion_tokens"
    )


@pytest.mark.asyncio
async def test_repeated_exhaustion_stops_at_ceiling_and_preserves_failure() -> None:
    budgets = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        budgets.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(400, json=EXHAUSTED)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        rows = rows_by_id(
            await run_live_checks(
                URL, "", model(), provider_fee=1, sats_to_usd=SATS_USD, client=client
            )
        )
    assert budgets == list(PROBE_TOKEN_BUDGETS)
    assert rows["usage.capture"]["status"] == "fail"
    assert len(rows["usage.capture"]["evidence"]["attempts"]) == len(
        PROBE_TOKEN_BUDGETS
    )
    assert rows["cache.reported"]["status"] == "warn"


@pytest.mark.parametrize(
    "status,message,current,expected",
    [
        (400, "max_tokens must be at least 16 for this deployment", 1, 32),
        (400, "max_tokens must be at least 2 for this deployment", 1, 32),
        (400, "max_tokens must be at least 9000", 32, None),
        (400, "context window exceeded", 32, None),
        (400, "unknown model", 32, None),
        (429, "increase max_tokens", 32, None),
    ],
)
def test_budget_corrections_are_narrow_and_bounded(
    status: int, message: str, current: int, expected: int | None
) -> None:
    assert (
        next_probe_budget(
            status,
            {"error": {"metadata": {"raw": json.dumps({"message": message})}}},
            current,
        )
        == expected
    )


@pytest.mark.asyncio
async def test_cache_retains_fallback_and_http_error_without_credentials() -> None:
    replies = iter(
        [
            httpx.Response(
                400, json={"error": "cache_control not allowed; key=private-test-key"}
            ),
            httpx.Response(200, json={"usage": CACHED}),
            httpx.Response(429, json={"error": "rate limited; key=private-test-key"}),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: next(replies))
    ) as client:
        probe = await probe_cache(URL, "private-test-key", "test-model", client=client)
    row = cache_reported_row(probe)
    assert probe.statuses == [200, 429]
    assert [a["status_code"] for a in row["evidence"]["attempts"]] == [400, 200, 429]
    assert [a["request_format"] for a in probe.attempts] == [
        "cache_control",
        "plain",
        "plain",
    ]
    assert "cache_control not allowed" in probe.attempts[0]["body"]
    assert "HTTP 429" in row["detail"] and "rate limited" in row["detail"]
    assert "no response body" not in row["detail"]
    assert "private-test-key" not in json.dumps(row)


@pytest.mark.asyncio
async def test_cache_decode_failure_has_bounded_body() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="not JSON " + "x" * 3000)
        )
    ) as client:
        probe = await probe_cache(URL, "", "test-model", client=client)
    row = cache_reported_row(probe)
    assert row["status"] == "fail"
    assert "JSONDecodeError" in row["detail"]
    assert all(len(a["body"]) <= 1001 for a in probe.attempts)
