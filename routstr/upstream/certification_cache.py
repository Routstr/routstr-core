"""Prompt-cache and margin certification for an upstream provider.

Three questions the one-token probe cannot answer:

* does the upstream *report* prompt-cache hits in a dialect the node parses,
* does the node bill cached reads at the discounted rate (client side), and
* does the node's charge cover what the upstream charged (node side).

The cache probe sends the same long system prompt twice; the second call is
the one expected to report cached reads. Calls go straight to the upstream
with ``httpx`` and never enter the billing path.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from ..core.logging import get_logger
from ..payment.cost_calculation import CostDataError, calculate_cost
from ..payment.usage import NormalizedUsage, normalize_usage
from .certification import (
    _PROBE_MAX_COST_MSATS,
    COST_TOLERANCE_MSATS,
    PROBE_MAX_TOKENS,
    PROBE_TIMEOUT_SECONDS,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    _expected_token_msats,
    _expected_usd_msats,
    _fixed_token_pricing_active,
    _reported_usd_cost,
    _token_rates,
    certification_row,
    probe_shape,
    safe_row,
    shape_body,
)

if TYPE_CHECKING:
    from ..payment.models import Model
    from .base import BaseUpstreamProvider

logger = get_logger(__name__)

# OpenAI caches prefixes of 1024+ tokens; Anthropic Haiku needs 2048+. The
# filler lands around 3000 tokens so every dialect can hit its threshold.
CACHE_PROBE_LINES = 220
CACHE_PROBE_QUESTION = "Reply with the single word: ok"

ROW_REPORTED = "cache.reported"
ROW_BILLING = "cache.billing"
ROW_MARGIN = "cost.margin"

TITLE_REPORTED = "Upstream reports prompt-cache hits"
TITLE_BILLING = "Cached tokens billed at the cache-read rate"
TITLE_MARGIN = "Node charge covers upstream cost"


def cache_probe_prefix() -> str:
    lines = [
        "You are a certification probe. Ignore the reference table below and "
        "answer the final question with one word."
    ]
    for index in range(CACHE_PROBE_LINES):
        lines.append(
            f"Reference row {index:04d}: token {index * 7919 % 10007} maps to "
            f"slot {index * 104729 % 1009} in region {index % 17}."
        )
    return "\n".join(lines)


@dataclass
class CacheProbeResult:
    """Two identical completions; the second should read from the cache."""

    chat_url: str
    request_format: str = "cache_control"
    endpoint_tag: str | None = None
    statuses: list[int | None] = field(default_factory=list)
    payloads: list[dict[str, Any] | None] = field(default_factory=list)
    errors: list[str | None] = field(default_factory=list)
    latencies_ms: list[float | None] = field(default_factory=list)

    @property
    def second_payload(self) -> dict[str, Any] | None:
        return self.payloads[1] if len(self.payloads) > 1 else None

    @property
    def second_error(self) -> str | None:
        if len(self.errors) > 1:
            return self.errors[1]
        return self.errors[0] if self.errors else "cache probe did not run"


def _request_body(
    model_id: str,
    prefix: str,
    fmt: str,
    endpoint_tag: str | None,
    token_field: str = "max_tokens",
) -> dict[str, Any]:
    system: Any
    if fmt == "cache_control":
        system = [
            {
                "type": "text",
                "text": prefix,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        system = prefix
    body: dict[str, Any] = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": CACHE_PROBE_QUESTION},
        ],
        token_field: PROBE_MAX_TOKENS,
        "stream": False,
    }
    if endpoint_tag:
        body["provider"] = {
            "order": [endpoint_tag],
            "allow_fallbacks": False,
        }
    return body


async def _post_completion(
    client: httpx.AsyncClient,
    url: str,
    body: Any,
    headers: dict[str, str],
    params: dict[str, str],
    timeout: float,
) -> tuple[int | None, dict[str, Any] | None, str | None, float]:
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            response = await client.post(url, json=body, headers=headers, params=params)
    except Exception as exc:  # noqa: BLE001 - transport failure is a row status
        latency = round((time.monotonic() - started) * 1000, 2)
        return None, None, f"{type(exc).__name__}: {exc}", latency
    latency = round((time.monotonic() - started) * 1000, 2)
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - any decode failure is the signal
        return response.status_code, None, f"{type(exc).__name__}: {exc}", latency
    if not isinstance(payload, dict):
        return (
            response.status_code,
            None,
            f"expected a JSON object, got {type(payload).__name__}",
            latency,
        )
    return response.status_code, payload, None, latency


def _record(
    result: CacheProbeResult,
    outcome: tuple[int | None, dict[str, Any] | None, str | None, float],
) -> None:
    status, payload, error, latency = outcome
    result.statuses.append(status)
    result.payloads.append(payload)
    result.errors.append(error)
    result.latencies_ms.append(latency)


def _is_2xx(status: int | None) -> bool:
    return status is not None and 200 <= status < 300


async def probe_cache(
    base_url: str,
    api_key: str,
    model_id: str,
    *,
    endpoint_tag: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    upstream: "BaseUpstreamProvider | None" = None,
    model: "Model | None" = None,
    token_limit_field: str = "max_tokens",
) -> CacheProbeResult:
    """Send the same long prompt twice.

    The first attempt marks the prefix with an Anthropic-style
    ``cache_control`` part. Upstreams that reject the part get a plain string
    retry on HTTP 400/422, and the second call mirrors whichever format
    succeeded. Each call's elapsed deadline includes the response body.
    """
    shape = probe_shape(base_url, api_key, upstream, model)
    result = CacheProbeResult(chat_url=shape.chat_url, endpoint_tag=endpoint_tag)
    prefix = cache_probe_prefix()

    owns_client = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=timeout)

    async def post(
        fmt: str,
    ) -> tuple[int | None, dict[str, Any] | None, str | None, float]:
        body = _request_body(model_id, prefix, fmt, endpoint_tag, token_limit_field)
        return await _post_completion(
            http,
            result.chat_url,
            shape_body(body, upstream, model),
            shape.headers,
            shape.chat_params,
            timeout,
        )

    try:
        first = await post("cache_control")
        if first[0] in (400, 422):
            result.request_format = "plain"
            first = await post("plain")
        _record(result, first)
        if not _is_2xx(first[0]):
            return result
        second = await post(result.request_format)
        _record(result, second)
    finally:
        if owns_client:
            await http.aclose()
    return result


def _raw_cache_keys(value: Any, path: str = "") -> list[str]:
    """Paths of positive numeric fields whose name mentions a cache."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if "cach" in str(key).lower() and isinstance(item, (int, float)):
                if not isinstance(item, bool) and item > 0:
                    found.append(child)
            found.extend(_raw_cache_keys(item, child))
    return found


def _usage_of(payload: dict[str, Any] | None) -> NormalizedUsage | None:
    if not isinstance(payload, dict):
        return None
    try:
        return normalize_usage(payload.get("usage"))
    except Exception:  # noqa: BLE001 - a malformed usage object is a row status
        return None


def cache_reported_row(probe: CacheProbeResult) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "url": probe.chat_url,
        "request_format": probe.request_format,
        "endpoint_tag": probe.endpoint_tag,
        "statuses": probe.statuses,
        "latencies_ms": probe.latencies_ms,
    }
    payload = probe.second_payload
    if payload is None or not _is_2xx(probe.statuses[-1] if probe.statuses else None):
        evidence["error"] = probe.second_error
        return certification_row(
            ROW_REPORTED,
            STATUS_FAIL,
            TITLE_REPORTED,
            f"The cache probe did not get two successful completions: "
            f"{probe.second_error or 'no response body'}.",
            evidence,
        )

    first_usage = _usage_of(probe.payloads[0])
    second_usage = _usage_of(payload)
    evidence["first_usage"] = first_usage.dict() if first_usage else None
    evidence["second_usage"] = second_usage.dict() if second_usage else None

    if second_usage is not None and second_usage.cache_read_tokens > 0:
        return certification_row(
            ROW_REPORTED,
            STATUS_OK,
            TITLE_REPORTED,
            f"The repeated prompt reported {second_usage.cache_read_tokens} "
            f"cached tokens (first call wrote {first_usage.cache_write_tokens if first_usage else 0}).",
            evidence,
        )

    known_write_fields = {
        "cache_creation_input_tokens",
        "prompt_tokens_details.cache_creation_tokens",
        "prompt_tokens_details.cache_write_tokens",
        "input_tokens_details.cache_write_tokens",
    }
    raw_keys = [
        key
        for key in _raw_cache_keys(payload.get("usage"))
        if key not in known_write_fields
    ]
    if raw_keys:
        evidence["unrecognised_cache_fields"] = raw_keys
        return certification_row(
            ROW_REPORTED,
            STATUS_FAIL,
            TITLE_REPORTED,
            "The upstream reported cache tokens under fields the node does not "
            f"parse ({', '.join(raw_keys)}); cached reads would be billed at "
            "the full input rate.",
            evidence,
        )
    return certification_row(
        ROW_REPORTED,
        STATUS_WARN,
        TITLE_REPORTED,
        "Two identical prompts produced no cache hit. Either the model does "
        "not support prompt caching or the upstream hides it; clients pay the "
        "full input rate on repeated prompts.",
        evidence,
    )


def cache_billing_row(
    *,
    model: Model,
    probe: CacheProbeResult,
    cost_data: Any,
    pricing_known: bool = True,
) -> dict[str, Any]:
    usage = _usage_of(probe.second_payload)
    evidence: dict[str, Any] = {"model_id": model.id}
    if usage is None or usage.cache_read_tokens <= 0:
        return certification_row(
            ROW_BILLING,
            STATUS_WARN,
            TITLE_BILLING,
            "No cached reads were reported, so there is nothing to price.",
            evidence,
        )
    if model.sats_pricing is None or not pricing_known:
        return certification_row(
            ROW_BILLING,
            STATUS_WARN,
            TITLE_BILLING,
            "No pricing is known for this model, so the cache discount cannot "
            "be verified.",
            evidence,
        )
    if isinstance(cost_data, CostDataError):
        evidence["error"] = cost_data.message
        return certification_row(
            ROW_BILLING,
            STATUS_FAIL,
            TITLE_BILLING,
            f"The cost engine could not price the cached completion: "
            f"{cost_data.message}.",
            evidence,
        )

    pricing = model.sats_pricing
    input_rate, _, cache_read_rate, _ = _token_rates(pricing)
    full_usage = NormalizedUsage(
        input_tokens=usage.input_tokens
        + usage.cache_read_tokens
        + usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
    )
    try:
        expected_total, _, _ = _expected_token_msats(pricing, usage)
        full_total, _, _ = _expected_token_msats(pricing, full_usage)
    except (ValueError, OverflowError) as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        return certification_row(
            ROW_BILLING,
            STATUS_FAIL,
            TITLE_BILLING,
            f"The expected charge could not be derived: {exc}.",
            evidence,
        )

    actual_total = int(cost_data.total_msats)
    reported_usd = _reported_usd_cost(probe.second_payload or {})
    evidence.update(
        {
            "usage": usage.dict(),
            "cache_read_rate_msats_per_1k": cache_read_rate,
            "input_rate_msats_per_1k": input_rate,
            "actual_total_msats": actual_total,
            "expected_total_msats": expected_total,
            "full_price_total_msats": full_total,
            "reported_usd": reported_usd or None,
        }
    )

    if reported_usd > 0:
        return certification_row(
            ROW_BILLING,
            STATUS_OK,
            TITLE_BILLING,
            f"Billed {actual_total} msats from the upstream-reported cost, "
            f"which already carries the cache discount "
            f"(full token price would be {full_total} msats).",
            evidence,
        )
    if abs(actual_total - expected_total) > COST_TOLERANCE_MSATS:
        return certification_row(
            ROW_BILLING,
            STATUS_FAIL,
            TITLE_BILLING,
            f"The engine charged {actual_total} msats but the configured cache "
            f"rate implies {expected_total} msats.",
            evidence,
        )
    if cache_read_rate <= 0.0 or cache_read_rate >= input_rate:
        reason = (
            "the node uses fixed per-1k pricing"
            if _fixed_token_pricing_active()
            else "no discounted cache-read rate is configured"
        )
        return certification_row(
            ROW_BILLING,
            STATUS_WARN,
            TITLE_BILLING,
            f"Cached reads are billed at the full input rate ({actual_total} "
            f"msats) because {reason}; clients pay more than the upstream "
            "charges.",
            evidence,
        )
    return certification_row(
        ROW_BILLING,
        STATUS_OK,
        TITLE_BILLING,
        f"Charged {actual_total} msats for {usage.cache_read_tokens} cached "
        f"tokens, {full_total - actual_total} msats below the full input price.",
        evidence,
    )


def cost_margin_row(
    *,
    model: Model,
    payloads: list[dict[str, Any] | None],
    provider_fee: float,
    sats_to_usd: float,
    pricing_known: bool = True,
    advertised_model: Model | None = None,
) -> dict[str, Any]:
    """Configured token pricing must cover what the upstream reports charging.

    Responses that carry a USD cost are billed from it, so they cannot lose
    money themselves; they are used here as a price sample. The configured
    token pricing is what every other path bills from (streams, upstreams
    that omit cost, the served ``/v1/models`` list), so a sample where it
    falls below the fee-adjusted upstream cost means those paths underprice.
    Upstreams that report no cost give no sample and the row stays a warn.

    ``model`` carries the pricing the proxy reserves and token-bills with. On
    a pinned path, ``advertised_model`` carries the endpoint's own rates; a
    covered margin whose advertised rates differ from the billed ones is a
    warn, since ``/v1/models/paths`` then shows a price the node does not bill.
    """
    advertised_pricing = (
        advertised_model.sats_pricing if advertised_model is not None else None
    )
    evidence: dict[str, Any] = {
        "model_id": model.id,
        "provider_fee": provider_fee,
        "sats_usd_price": sats_to_usd,
        "pricing_basis": "model pricing the proxy reserves and token-bills with",
        "samples": [],
    }
    if model.sats_pricing is None or not pricing_known:
        return certification_row(
            ROW_MARGIN,
            STATUS_WARN,
            TITLE_MARGIN,
            "No pricing is known for this model, so the margin cannot be verified.",
            evidence,
        )

    samples: list[dict[str, Any]] = []
    short: list[str] = []
    mismatched: list[str] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        reported_usd = _reported_usd_cost(payload)
        usage = _usage_of(payload)
        if reported_usd <= 0 or usage is None:
            continue
        try:
            configured_total, _, _ = _expected_token_msats(model.sats_pricing, usage)
            upstream_total = _expected_usd_msats(
                reported_usd, provider_fee, sats_to_usd
            )
            advertised_total = (
                _expected_token_msats(advertised_pricing, usage)[0]
                if advertised_pricing is not None
                else None
            )
        except (ValueError, OverflowError) as exc:
            evidence["error"] = f"{type(exc).__name__}: {exc}"
            return certification_row(
                ROW_MARGIN,
                STATUS_FAIL,
                TITLE_MARGIN,
                f"The margin could not be derived: {exc}.",
                evidence,
            )
        sample: dict[str, Any] = {
            "usage": usage.dict(),
            "reported_usd": reported_usd,
            "upstream_msats_with_fee": upstream_total,
            "configured_msats": configured_total,
        }
        if advertised_total is not None:
            sample["advertised_msats"] = advertised_total
            if abs(advertised_total - configured_total) > COST_TOLERANCE_MSATS:
                mismatched.append(f"{advertised_total} vs {configured_total}")
        samples.append(sample)
        if configured_total + COST_TOLERANCE_MSATS < upstream_total:
            short.append(f"{configured_total} < {upstream_total}")
    evidence["samples"] = samples

    if not samples:
        return certification_row(
            ROW_MARGIN,
            STATUS_WARN,
            TITLE_MARGIN,
            "The upstream does not report a cost, so the margin cannot be "
            "verified live. Keep configured prices at or above the upstream's "
            "list price.",
            evidence,
        )
    if short:
        return certification_row(
            ROW_MARGIN,
            STATUS_FAIL,
            TITLE_MARGIN,
            "Configured pricing is below the upstream's reported cost "
            f"(configured < upstream msats: {'; '.join(short)}); token-billed "
            "requests lose money.",
            evidence,
        )
    if mismatched:
        return certification_row(
            ROW_MARGIN,
            STATUS_WARN,
            TITLE_MARGIN,
            f"Configured pricing covers the upstream's reported cost on "
            f"{len(samples)} sampled completion(s), but this path advertises "
            f"different endpoint rates (advertised vs billed msats: "
            f"{'; '.join(mismatched)}); the proxy reserves and token-bills "
            "pinned requests with the model's own pricing.",
            evidence,
        )
    return certification_row(
        ROW_MARGIN,
        STATUS_OK,
        TITLE_MARGIN,
        f"Configured pricing covers the upstream's reported cost on "
        f"{len(samples)} sampled completion(s).",
        evidence,
    )


async def _price_payload(
    payload: dict[str, Any] | None, model: Model, provider_fee: float
) -> Any:
    if payload is None:
        return CostDataError(
            message="the cache probe did not succeed", code="no_completion"
        )
    try:
        return await calculate_cost(
            payload, _PROBE_MAX_COST_MSATS, model_obj=model, provider_fee=provider_fee
        )
    except Exception as exc:  # noqa: BLE001 - a raising engine is a fail row
        return CostDataError(
            message=f"{type(exc).__name__}: {exc}", code="pricing_error"
        )


async def run_cache_checks(
    base_url: str,
    api_key: str,
    model: Model,
    *,
    provider_fee: float,
    sats_to_usd: float,
    probe_payload: dict[str, Any] | None,
    client: httpx.AsyncClient | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    pricing_known: bool = True,
    endpoint_tag: str | None = None,
    upstream: "BaseUpstreamProvider | None" = None,
    advertised_model: Model | None = None,
    token_limit_field: str = "max_tokens",
) -> list[dict[str, Any]]:
    """Run the cache probe and build the three cache/margin rows."""
    probe = await probe_cache(
        base_url,
        api_key,
        model.id,
        endpoint_tag=endpoint_tag,
        client=client,
        timeout=timeout,
        upstream=upstream,
        model=model,
        token_limit_field=token_limit_field,
    )
    cost_data = await _price_payload(probe.second_payload, model, provider_fee)
    return [
        safe_row(ROW_REPORTED, TITLE_REPORTED, lambda: cache_reported_row(probe)),
        safe_row(
            ROW_BILLING,
            TITLE_BILLING,
            lambda: cache_billing_row(
                model=model,
                probe=probe,
                cost_data=cost_data,
                pricing_known=pricing_known,
            ),
        ),
        safe_row(
            ROW_MARGIN,
            TITLE_MARGIN,
            lambda: cost_margin_row(
                model=model,
                payloads=[probe_payload, *probe.payloads],
                provider_fee=provider_fee,
                sats_to_usd=sats_to_usd,
                pricing_known=pricing_known,
                advertised_model=advertised_model,
            ),
        ),
    ]


def skipped_cache_rows(reason: str) -> list[dict[str, Any]]:
    return [
        certification_row(ROW_REPORTED, STATUS_WARN, TITLE_REPORTED, reason, {}),
        certification_row(ROW_BILLING, STATUS_WARN, TITLE_BILLING, reason, {}),
        certification_row(ROW_MARGIN, STATUS_WARN, TITLE_MARGIN, reason, {}),
    ]
