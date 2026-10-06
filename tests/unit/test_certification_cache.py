"""Unit tests for the cache and margin rows in
routstr.upstream.certification_cache — no DB, network only via respx."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from routstr.payment.cost_calculation import CostData, CostDataError
from routstr.upstream.certification import (
    PROBE_MAX_TOKENS,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
)
from routstr.upstream.certification_cache import (
    CacheProbeResult,
    _raw_cache_keys,
    cache_billing_row,
    cache_probe_prefix,
    cache_reported_row,
    cost_margin_row,
    probe_cache,
    skipped_cache_rows,
)

SATS_USD = 0.0005
CHAT_URL = "https://upstream.example/v1/chat/completions"


def _model(
    prompt: float = 1.4e-7,
    completion: float = 2.8e-7,
    cache_read: float = 0.0,
    sats_usd: float = SATS_USD,
) -> Any:
    from routstr.payment.models import (
        Architecture,
        Model,
        Pricing,
        _update_model_sats_pricing,
    )

    model = Model(
        id="test-model",
        name="test-model",
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="unknown",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=prompt, completion=completion, input_cache_read=cache_read
        ),
        sats_pricing=None,
        per_request_limits=None,
        top_provider=None,
        enabled=True,
        upstream_provider_id=None,
        canonical_slug=None,
    )
    return _update_model_sats_pricing(model, sats_usd)


def _payload(usage: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {"choices": [{"message": {"content": "ok"}}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _probe(
    payloads: list[dict[str, Any] | None],
    statuses: list[int | None] | None = None,
) -> CacheProbeResult:
    statuses = statuses if statuses is not None else [200] * len(payloads)
    return CacheProbeResult(
        chat_url=CHAT_URL,
        statuses=statuses,
        payloads=payloads,
        errors=[None] * len(payloads),
        latencies_ms=[1.0] * len(payloads),
    )


def _cost(total: int) -> CostData:
    return CostData(base_msats=0, input_msats=total, output_msats=0, total_msats=total)


CACHED = {
    "prompt_tokens": 3000,
    "completion_tokens": 1,
    "prompt_tokens_details": {"cached_tokens": 2900},
}
UNCACHED = {"prompt_tokens": 3000, "completion_tokens": 1}


class TestPrefix:
    def test_prefix_is_long_and_deterministic(self) -> None:
        prefix = cache_probe_prefix()
        assert len(prefix) > 8000
        assert prefix == cache_probe_prefix()


class TestRawCacheKeys:
    def test_finds_nested_positive_cache_fields(self) -> None:
        usage = {"prompt_tokens": 5, "details": {"cache_hits": 3, "cached": 0}}
        assert _raw_cache_keys(usage) == ["details.cache_hits"]

    def test_ignores_bool_and_non_numeric(self) -> None:
        assert _raw_cache_keys({"cached": True, "cache_key": "abc"}) == []


class TestCacheReportedRow:
    def test_ok_when_second_call_reports_cached_tokens(self) -> None:
        row = cache_reported_row(_probe([_payload(UNCACHED), _payload(CACHED)]))
        assert row["status"] == STATUS_OK
        assert row["evidence"]["second_usage"]["cache_read_tokens"] == 2900

    def test_warn_when_no_cache_hit(self) -> None:
        row = cache_reported_row(_probe([_payload(UNCACHED), _payload(UNCACHED)]))
        assert row["status"] == STATUS_WARN

    def test_fail_when_cache_reported_under_unknown_field(self) -> None:
        second = _payload(
            {"prompt_tokens": 3000, "completion_tokens": 1, "cache_hit_tokens": 2900}
        )
        row = cache_reported_row(_probe([_payload(UNCACHED), second]))
        assert row["status"] == STATUS_FAIL
        assert row["evidence"]["unrecognised_cache_fields"] == ["cache_hit_tokens"]

    def test_fail_when_first_call_failed(self) -> None:
        probe = CacheProbeResult(
            chat_url=CHAT_URL,
            statuses=[None],
            payloads=[None],
            errors=["ConnectError: boom"],
            latencies_ms=[1.0],
        )
        row = cache_reported_row(probe)
        assert row["status"] == STATUS_FAIL
        assert "ConnectError" in row["detail"]

    def test_fail_when_second_call_non_2xx(self) -> None:
        row = cache_reported_row(
            _probe([_payload(UNCACHED), {"error": "rate limited"}], [200, 429])
        )
        assert row["status"] == STATUS_FAIL


class TestCacheBillingRow:
    def test_warn_when_nothing_cached(self) -> None:
        row = cache_billing_row(
            model=_model(), probe=_probe([_payload(UNCACHED)] * 2), cost_data=_cost(1)
        )
        assert row["status"] == STATUS_WARN

    def test_warn_when_pricing_unknown(self) -> None:
        row = cache_billing_row(
            model=_model(),
            probe=_probe([_payload(UNCACHED), _payload(CACHED)]),
            cost_data=_cost(1),
            pricing_known=False,
        )
        assert row["status"] == STATUS_WARN

    def test_fail_on_engine_error(self) -> None:
        row = cache_billing_row(
            model=_model(),
            probe=_probe([_payload(UNCACHED), _payload(CACHED)]),
            cost_data=CostDataError(message="nope", code="pricing_error"),
        )
        assert row["status"] == STATUS_FAIL

    def test_ok_with_discounted_rate(self) -> None:
        # 280 msats/1k input, 28 msats/1k cached, 560 msats/1k output:
        # 100*0.28 + 2900*0.028 + 1*0.56 = 109.76 -> 110
        row = cache_billing_row(
            model=_model(cache_read=1.4e-8),
            probe=_probe([_payload(UNCACHED), _payload(CACHED)]),
            cost_data=_cost(110),
        )
        assert row["status"] == STATUS_OK, row
        assert row["evidence"]["full_price_total_msats"] == 841

    def test_warn_when_cached_billed_at_full_rate(self) -> None:
        row = cache_billing_row(
            model=_model(),
            probe=_probe([_payload(UNCACHED), _payload(CACHED)]),
            cost_data=_cost(841),
        )
        assert row["status"] == STATUS_WARN
        assert "full input rate" in row["detail"]

    def test_fail_when_engine_disagrees(self) -> None:
        row = cache_billing_row(
            model=_model(cache_read=1.4e-8),
            probe=_probe([_payload(UNCACHED), _payload(CACHED)]),
            cost_data=_cost(841),
        )
        assert row["status"] == STATUS_FAIL

    def test_ok_when_upstream_reports_cost(self) -> None:
        cached = _payload({**CACHED, "cost": 5e-5})
        row = cache_billing_row(
            model=_model(),
            probe=_probe([_payload(UNCACHED), cached]),
            cost_data=_cost(100),
            provider_fee=1.0,
            sats_to_usd=SATS_USD,
        )
        assert row["status"] == STATUS_OK
        assert row["evidence"]["reported_usd"] == 5e-5


class TestCostMarginRow:
    def _row(
        self, payloads: list[Any], fee: float = 1.0, model: Any = None
    ) -> dict[str, Any]:
        return cost_margin_row(
            model=model or _model(),
            payloads=payloads,
            provider_fee=fee,
            sats_to_usd=SATS_USD,
        )

    def test_warn_when_no_cost_reported(self) -> None:
        row = self._row([_payload(UNCACHED), None])
        assert row["status"] == STATUS_WARN
        assert row["evidence"]["samples"] == []

    def test_ok_when_configured_covers_upstream(self) -> None:
        # configured: 5*0.28 + 1*0.56 = 1.96 -> 2 msats; upstream 9e-7 USD -> 2
        payload = _payload({"prompt_tokens": 5, "completion_tokens": 1, "cost": 9e-7})
        row = self._row([payload])
        assert row["status"] == STATUS_OK, row
        assert row["evidence"]["samples"][0]["upstream_msats_with_fee"] == 2

    def test_fail_when_upstream_costs_more(self) -> None:
        payload = _payload({"prompt_tokens": 5, "completion_tokens": 1, "cost": 1e-3})
        row = self._row([payload])
        assert row["status"] == STATUS_FAIL
        assert "below raw upstream cost" in row["detail"]
        assert "does not measure client debits" in row["detail"]

    def test_fee_scales_upstream_cost(self) -> None:
        payload = _payload({"prompt_tokens": 5, "completion_tokens": 1, "cost": 9e-7})
        assert self._row([payload], fee=1.0)["status"] == STATUS_OK
        marked_up = self._row([payload], fee=2.0)
        assert marked_up["status"] == STATUS_FAIL
        assert "Raw upstream cost is covered" in marked_up["detail"]
        assert "lose money" not in marked_up["detail"]
        sample = marked_up["evidence"]["samples"][0]
        assert sample["upstream_msats"] == 2
        assert sample["upstream_msats_with_fee"] == 4
        assert sample["token_estimate_minus_upstream_msats"] == 0
        assert sample["token_estimate_minus_fee_target_msats"] == -2

    def test_deepseek_endpoint_price_exceeds_configured_model_price(self) -> None:
        sats_usd = 0.0008616302499999999
        model = _model(
            prompt=4e-8,
            completion=2e-7,
            cache_read=4e-9,
            sats_usd=sats_usd,
        )
        payloads: list[dict[str, Any] | None] = [
            _payload(
                {
                    "prompt_tokens": 31,
                    "completion_tokens": 1,
                    "cost": 0.00000459,
                }
            ),
            _payload(
                {
                    "prompt_tokens": 4442,
                    "completion_tokens": 1,
                    "cost": 0.00057802,
                }
            ),
            _payload(
                {
                    "prompt_tokens": 4442,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 4352},
                    "cost": 0.00005578,
                }
            ),
        ]

        row = cost_margin_row(
            model=model,
            payloads=payloads,
            provider_fee=0.4,
            sats_to_usd=sats_usd,
        )

        assert row["status"] == STATUS_FAIL
        assert [
            (sample["upstream_msats_with_fee"], sample["configured_msats"])
            for sample in row["evidence"]["samples"]
        ] == [(3, 2), (269, 207), (26, 25)]
        assert "207 < 269" in row["detail"]
        assert "2 < 3" not in row["detail"]
        assert "25 < 26" not in row["detail"]

    def test_one_msat_margin_gap_is_rounding_tolerance(self) -> None:
        sats_usd = 0.0008616302499999999
        model = _model(
            prompt=4e-8,
            completion=2e-7,
            cache_read=4e-9,
            sats_usd=sats_usd,
        )
        payloads: list[dict[str, Any] | None] = [
            _payload(
                {
                    "prompt_tokens": 31,
                    "completion_tokens": 1,
                    "cost": 0.00000459,
                }
            ),
            _payload(
                {
                    "prompt_tokens": 4442,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 4352},
                    "cost": 0.00005578,
                }
            ),
        ]

        row = cost_margin_row(
            model=model,
            payloads=payloads,
            provider_fee=0.4,
            sats_to_usd=sats_usd,
        )

        assert row["status"] == STATUS_OK
        assert row["title"] == "Token estimate covers fee-adjusted target"
        assert "raw upstream cost exceeds" in row["detail"]

    def test_warn_when_pricing_unknown(self) -> None:
        payload = _payload({"prompt_tokens": 5, "completion_tokens": 1, "cost": 9e-7})
        row = cost_margin_row(
            model=_model(),
            payloads=[payload],
            provider_fee=1.0,
            sats_to_usd=SATS_USD,
            pricing_known=False,
        )
        assert row["status"] == STATUS_WARN


class TestSkippedRows:
    def test_three_warn_rows(self) -> None:
        rows = skipped_cache_rows("Skipped")
        assert [r["id"] for r in rows] == [
            "cache.reported",
            "cache.billing",
            "cost.margin",
        ]
        assert all(r["status"] == STATUS_WARN for r in rows)


class TestProbeCache:
    @pytest.mark.asyncio
    @respx.mock
    async def test_sends_same_prompt_twice_with_cache_control(self) -> None:
        route = respx.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=_payload(CACHED))
        )
        async with httpx.AsyncClient() as client:
            result = await probe_cache(
                "https://upstream.example/v1", "k", "m", client=client
            )
        assert result.request_format == "cache_control"
        assert route.call_count == 2
        first, second = (json.loads(c.request.content) for c in route.calls)
        assert first == second
        system = first["messages"][0]["content"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert first["max_tokens"] == PROBE_MAX_TOKENS
        assert route.calls[0].request.headers["Authorization"] == "Bearer k"

    @pytest.mark.asyncio
    @respx.mock
    async def test_pins_both_requests_to_one_endpoint(self) -> None:
        route = respx.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=_payload(CACHED))
        )
        async with httpx.AsyncClient() as client:
            await probe_cache(
                "https://upstream.example/v1",
                "",
                "m",
                endpoint_tag="azure/swedencentral",
                client=client,
            )
        assert route.call_count == 2
        for call in route.calls:
            body = json.loads(call.request.content)
            assert body["provider"] == {
                "order": ["azure/swedencentral"],
                "allow_fallbacks": False,
            }

    @pytest.mark.asyncio
    @respx.mock
    async def test_falls_back_to_plain_system_on_rejection(self) -> None:
        route = respx.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(400, json={"error": "cache_control not allowed"}),
                httpx.Response(200, json=_payload(UNCACHED)),
                httpx.Response(200, json=_payload(CACHED)),
            ]
        )
        async with httpx.AsyncClient() as client:
            result = await probe_cache(
                "https://upstream.example/v1", "", "m", client=client
            )
        assert result.request_format == "plain"
        assert route.call_count == 3
        assert result.statuses == [200, 200]
        body = json.loads(route.calls[1].request.content)
        assert isinstance(body["messages"][0]["content"], str)

    @pytest.mark.asyncio
    @respx.mock
    async def test_stops_after_failed_first_call(self) -> None:
        route = respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("boom"))
        async with httpx.AsyncClient() as client:
            result = await probe_cache(
                "https://upstream.example/v1", "", "m", client=client
            )
        assert route.call_count == 1
        assert result.payloads == [None]
        assert "ConnectError" in (result.second_error or "")


class TestErrorBranches:
    @pytest.mark.asyncio
    @respx.mock
    async def test_non_json_and_list_bodies_are_errors(self) -> None:
        respx.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, content=b"not json"),
                httpx.Response(200, json=[1, 2]),
            ]
        )
        async with httpx.AsyncClient() as client:
            result = await probe_cache(
                "https://upstream.example/v1", "", "m", client=client
            )
        assert result.payloads == [None, None]
        assert result.errors[0] is not None
        assert "list" in (result.errors[1] or "")

    def test_malformed_usage_object_is_not_a_hit(self) -> None:
        second = _payload({"prompt_tokens": {"nested": True}})
        row = cache_reported_row(_probe([_payload(UNCACHED), second]))
        assert row["status"] == STATUS_WARN

    def test_billing_fails_on_non_finite_rate(self) -> None:
        row = cache_billing_row(
            model=_model(prompt=float("inf"), cache_read=1.4e-8),
            probe=_probe([_payload(UNCACHED), _payload(CACHED)]),
            cost_data=_cost(1),
        )
        assert row["status"] == STATUS_FAIL
        assert "error" in row["evidence"]

    @pytest.mark.asyncio
    async def test_billing_warns_under_fixed_pricing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from routstr.core.settings import settings
        from routstr.payment.cost_calculation import calculate_cost

        fixed_in, fixed_out = 2.0, 3.0
        monkeypatch.setattr(settings, "fixed_pricing", True)
        monkeypatch.setattr(settings, "fixed_per_1k_input_tokens", fixed_in)
        monkeypatch.setattr(settings, "fixed_per_1k_output_tokens", fixed_out)
        monkeypatch.setattr("routstr.payment.price.SATS_USD_PRICE", SATS_USD)

        model = _model(cache_read=1.4e-8)
        payload = _payload(CACHED)
        cost = await calculate_cost(payload, 10**9, model_obj=model, provider_fee=1.0)
        row = cache_billing_row(
            model=model,
            probe=_probe([_payload(UNCACHED), payload]),
            cost_data=cost,
        )
        assert row["status"] == STATUS_WARN, row
        assert "fixed per-1k pricing" in row["detail"]
        assert row["evidence"]["input_rate_msats_per_1k"] == fixed_in * 1000
        assert row["evidence"]["cache_read_rate_msats_per_1k"] == fixed_in * 1000

    def test_margin_fails_on_zero_sats_price(self) -> None:
        payload = _payload({"prompt_tokens": 5, "completion_tokens": 1, "cost": 9e-7})
        row = cost_margin_row(
            model=_model(), payloads=[payload], provider_fee=1.0, sats_to_usd=0.0
        )
        assert row["status"] == STATUS_FAIL

    @pytest.mark.asyncio
    async def test_engine_raise_becomes_billing_fail(self) -> None:
        from unittest.mock import patch

        from routstr.upstream.certification_cache import run_cache_checks

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("engine exploded")

        with (
            patch(
                "routstr.upstream.certification_cache.probe_cache",
                return_value=_probe([_payload(UNCACHED), _payload(CACHED)]),
            ),
            patch("routstr.upstream.certification_cache.calculate_cost", _boom),
        ):
            rows = await run_cache_checks(
                "https://upstream.example/v1",
                "",
                _model(),
                provider_fee=1.0,
                sats_to_usd=SATS_USD,
                probe_payload=_payload(UNCACHED),
            )
        by_id = {r["id"]: r for r in rows}
        assert by_id["cache.billing"]["status"] == STATUS_FAIL
        assert "engine exploded" in by_id["cache.billing"]["detail"]
