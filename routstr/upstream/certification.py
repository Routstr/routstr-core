"""Live certification checks for an upstream provider endpoint.

Extends the read-only pricing rows, which never touch the network, with the
ones that must: a ``/models`` heartbeat and a one-token completion.

Probes call the upstream directly with ``httpx``, never through the node's
billing path — no reservation, no Cashu, at most one token of upstream spend.
They sit behind ``POST …/certify`` rather than the read-only ``GET …/report``
because they can block for the length of the timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from ..core.logging import get_logger
from ..payment.cost_calculation import calculate_cost
from ..payment.rates import coerce_rate
from ..payment.usage import normalize_usage

if TYPE_CHECKING:
    from ..payment.models import Model

logger = get_logger(__name__)

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

TICKS = {STATUS_OK: "☑️", STATUS_WARN: "⚠️", STATUS_FAIL: "❌"}

# Bounded so a dead upstream fails the row rather than wedging the request.
PROBE_TIMEOUT_SECONDS = 15.0

# Ceiling for the caller-supplied timeout override.
MAX_PROBE_TIMEOUT_SECONDS = 60.0

# The cheapest request that still exercises the usage/cost path.
PROBE_MAX_TOKENS = 1
PROBE_PROMPT = "ping"

# ``calculate_cost`` demands a reservation ceiling; any value at or above the
# real charge behaves identically.
_PROBE_MAX_COST_MSATS = 1_000_000_000

# ``_calculate_from_tokens`` truncates the output component and folds the
# remainder into the input one, so a one-msat difference is arithmetic.
COST_TOLERANCE_MSATS = 1


def certification_row(
    row_id: str,
    status: str,
    title: str,
    detail: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one row, coercing ``evidence`` to a dict so the row contract
    holds by construction rather than by caller discipline."""
    return {
        "id": row_id,
        "status": status,
        "title": title,
        "detail": detail,
        "evidence": evidence if isinstance(evidence, dict) else {},
    }


def safe_row(
    row_id: str,
    title: str,
    builder: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Run a row builder, turning any raise into a ``fail`` row: the report is
    the diagnostic, so it must never be the thing that 500s."""
    try:
        return builder()
    except Exception as exc:  # noqa: BLE001 - a raising check is a row status
        described = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Certification check raised",
            extra={"row_id": row_id, "error": described},
        )
        return certification_row(
            row_id,
            STATUS_FAIL,
            title,
            f"The {row_id} check could not run: {described}.",
            {"error": described},
        )


# Operator-facing goals mapped onto the rows that decide them: ``ok`` only when
# every named row is ``ok``, ``fail`` if any fails, ``warn`` otherwise.
CHECKLIST_GOALS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "heartbeat",
        "Heartbeat — endpoint responds and is online",
        ("endpoint.reachable",),
    ),
    (
        "usage_data",
        "Usage data — tokens and requests captured",
        ("usage.capture",),
    ),
    (
        "cost_data",
        "Cost data — prompt and completion cost calculated",
        ("cost.prompt_completion",),
    ),
    (
        "pricing_v1_models",
        "Pricing in /v1/models — cost updates reflected in the models list",
        ("pricing.served_matches_configured", "pricing.enabled_models_served"),
    ),
)


def build_checklist(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {row["id"]: row for row in rows}
    checklist: list[dict[str, Any]] = []
    for goal, label, row_ids in CHECKLIST_GOALS:
        present = [by_id[row_id]["status"] for row_id in row_ids if row_id in by_id]
        if not present:
            status = STATUS_WARN
        elif any(item == STATUS_FAIL for item in present):
            status = STATUS_FAIL
        elif all(item == STATUS_OK for item in present):
            status = STATUS_OK
        else:
            status = STATUS_WARN
        checklist.append(
            {
                "goal": goal,
                "label": label,
                "status": status,
                "tick": TICKS[status],
                "rows": [row_id for row_id in row_ids if row_id in by_id],
            }
        )
    return checklist


@dataclass
class ProbeResult:
    """Raw outcome of the two live HTTP calls a probe makes."""

    base_url: str
    models_url: str
    chat_url: str
    models_status: int | None = None
    models_payload: dict[str, Any] | None = None
    models_error: str | None = None
    models_latency_ms: float | None = None
    chat_status: int | None = None
    chat_payload: dict[str, Any] | None = None
    chat_error: str | None = None
    chat_latency_ms: float | None = None


async def probe_upstream(
    base_url: str,
    api_key: str,
    model_id: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Call the upstream's ``/models`` and a one-token completion.

    A transport failure on either call is recorded on the result rather
    than raised: a dead upstream is a ``fail`` row, not a failed request.
    """
    base = base_url.rstrip("/")
    result = ProbeResult(
        base_url=base_url,
        models_url=f"{base}/models",
        chat_url=f"{base}/chat/completions",
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)

    try:
        started = time.monotonic()
        try:
            response = await client.get(result.models_url, headers=headers)
            result.models_status = response.status_code
            result.models_latency_ms = round((time.monotonic() - started) * 1000, 2)
            try:
                body = response.json()
            except Exception as exc:  # noqa: BLE001 - any decode failure is the signal
                result.models_error = f"{type(exc).__name__}: {exc}"
            else:
                if isinstance(body, dict):
                    result.models_payload = body
                else:
                    result.models_error = (
                        f"expected a JSON object, got {type(body).__name__}"
                    )
        except Exception as exc:  # noqa: BLE001 - transport failure is a row status
            result.models_error = f"{type(exc).__name__}: {exc}"
            result.models_latency_ms = round((time.monotonic() - started) * 1000, 2)

        started = time.monotonic()
        if not model_id:
            return result
        request_body = {
            "model": model_id,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": PROBE_MAX_TOKENS,
            "stream": False,
        }
        try:
            response = await client.post(
                result.chat_url, json=request_body, headers=headers
            )
            result.chat_status = response.status_code
            result.chat_latency_ms = round((time.monotonic() - started) * 1000, 2)
            try:
                payload = response.json()
            except Exception as exc:  # noqa: BLE001 - any decode failure is the signal
                result.chat_error = f"{type(exc).__name__}: {exc}"
            else:
                if isinstance(payload, dict):
                    result.chat_payload = payload
                else:
                    result.chat_error = (
                        f"expected a JSON object, got {type(payload).__name__}"
                    )
        except Exception as exc:  # noqa: BLE001 - transport failure is a row status
            result.chat_error = f"{type(exc).__name__}: {exc}"
            result.chat_latency_ms = round((time.monotonic() - started) * 1000, 2)
    finally:
        if owns_client:
            await client.aclose()

    return result


# Row builders are pure: the network lives only in ``probe_upstream`` and
# ``run_live_checks``, so every verdict is testable without a socket.


def endpoint_validity_row(base_url: str) -> dict[str, Any]:
    parsed = urlparse(base_url or "")
    problems: list[str] = []
    if parsed.scheme not in ("http", "https"):
        problems.append(f"scheme {parsed.scheme!r} is not http or https")
    # ``netloc`` is truthy for a hostless authority like ``http://:8080``;
    # only ``.hostname`` answers whether there is a host to connect to.
    if not parsed.hostname:
        problems.append("no host component")
    evidence: dict[str, Any] = {
        "base_url": base_url,
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "path": parsed.path,
    }
    if problems:
        return certification_row(
            "endpoint.validity",
            STATUS_FAIL,
            "Upstream URL is well-formed",
            "The configured base URL is not a usable http(s) endpoint: "
            + "; ".join(problems)
            + ".",
            evidence,
        )
    return certification_row(
        "endpoint.validity",
        STATUS_OK,
        "Upstream URL is well-formed",
        f"{parsed.scheme}://{parsed.netloc} is a valid endpoint.",
        evidence,
    )


def heartbeat_row(probe: ProbeResult) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "url": probe.models_url,
        "status_code": probe.models_status,
        "latency_ms": probe.models_latency_ms,
    }
    if probe.models_status is None:
        evidence["error"] = probe.models_error
        return certification_row(
            "endpoint.reachable",
            STATUS_FAIL,
            "Endpoint responds",
            f"No response from {probe.models_url}: {probe.models_error}.",
            evidence,
        )
    if 200 <= probe.models_status < 300:
        return certification_row(
            "endpoint.reachable",
            STATUS_OK,
            "Endpoint responds",
            f"{probe.models_url} answered {probe.models_status} in "
            f"{probe.models_latency_ms} ms.",
            evidence,
        )
    return certification_row(
        "endpoint.reachable",
        STATUS_FAIL,
        "Endpoint responds",
        f"{probe.models_url} answered {probe.models_status}.",
        evidence,
    )


def models_payload_row(probe: ProbeResult) -> dict[str, Any]:
    payload = probe.models_payload
    if not isinstance(payload, dict):
        return certification_row(
            "endpoint.models_payload",
            STATUS_FAIL,
            "Models payload has the expected shape",
            f"Could not read a JSON object from {probe.models_url}: "
            f"{probe.models_error or type(payload).__name__}.",
            {"url": probe.models_url, "error": probe.models_error},
        )

    data = payload.get("data")
    if not isinstance(data, list):
        return certification_row(
            "endpoint.models_payload",
            STATUS_FAIL,
            "Models payload has the expected shape",
            f'Expected a top-level "data" list, got {type(data).__name__}.',
            {
                "url": probe.models_url,
                "top_level_keys": sorted(payload.keys()),
            },
        )

    ids = [
        item["id"]
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    ]
    evidence: dict[str, Any] = {
        "url": probe.models_url,
        "model_count": len(data),
        "usable_ids": len(ids),
        "sample_ids": ids[:5],
    }
    if not ids:
        return certification_row(
            "endpoint.models_payload",
            STATUS_FAIL,
            "Models payload has the expected shape",
            f'The "data" list carries no entry with a non-empty string "id" '
            f"({len(data)} entries).",
            evidence,
        )
    return certification_row(
        "endpoint.models_payload",
        STATUS_OK,
        "Models payload has the expected shape",
        f"{len(ids)} of {len(data)} entries carry a string id.",
        evidence,
    )


def usage_capture_row(probe: ProbeResult) -> dict[str, Any]:
    """Check a completion comes back with token usage the node can bill on.

    A missing ``usage`` object means the node has nothing to price and the
    request settles for free. Broken, but still usable, so ``warn``.
    """
    evidence: dict[str, Any] = {
        "url": probe.chat_url,
        "status_code": probe.chat_status,
        "latency_ms": probe.chat_latency_ms,
    }
    if probe.chat_status is None:
        evidence["error"] = probe.chat_error
        return certification_row(
            "usage.capture",
            STATUS_FAIL,
            "Token usage captured from a completion",
            f"No response from {probe.chat_url}: {probe.chat_error}.",
            evidence,
        )
    if not 200 <= probe.chat_status < 300:
        evidence["body"] = _truncate(probe.chat_payload)
        return certification_row(
            "usage.capture",
            STATUS_FAIL,
            "Token usage captured from a completion",
            f"{probe.chat_url} answered {probe.chat_status} for a "
            f"{PROBE_MAX_TOKENS}-token probe.",
            evidence,
        )
    if probe.chat_payload is None or not isinstance(probe.chat_payload, dict):
        evidence["error"] = probe.chat_error
        return certification_row(
            "usage.capture",
            STATUS_FAIL,
            "Token usage captured from a completion",
            f"The completion body was not a JSON object: "
            f"{probe.chat_error or type(probe.chat_payload).__name__}.",
            evidence,
        )

    raw_usage = probe.chat_payload.get("usage")
    try:
        normalized = normalize_usage(raw_usage)
    except Exception as exc:  # noqa: BLE001 - a malformed usage object is a row status
        evidence["usage"] = _truncate(raw_usage)
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        return certification_row(
            "usage.capture",
            STATUS_FAIL,
            "Token usage captured from a completion",
            f"The completion's usage object could not be read: "
            f"{type(exc).__name__}: {exc}.",
            evidence,
        )
    evidence["usage"] = raw_usage
    if normalized is None:
        return certification_row(
            "usage.capture",
            STATUS_WARN,
            "Token usage captured from a completion",
            'The completion carried no "usage" object, so the node has no '
            "token counts to bill on and the request would settle as (0+0).",
            evidence,
        )
    evidence["input_tokens"] = normalized.input_tokens
    evidence["output_tokens"] = normalized.output_tokens
    if normalized.input_tokens <= 0 and normalized.output_tokens <= 0:
        return certification_row(
            "usage.capture",
            STATUS_WARN,
            "Token usage captured from a completion",
            "The completion reported a usage object with zero tokens in both "
            "directions.",
            evidence,
        )
    return certification_row(
        "usage.capture",
        STATUS_OK,
        "Token usage captured from a completion",
        f"Captured {normalized.input_tokens} input and "
        f"{normalized.output_tokens} output tokens.",
        evidence,
    )


def _truncate(value: Any, limit: int = 400) -> Any:
    """Clip an upstream body so one bad response cannot bloat the report."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _reported_usd_cost(payload: dict[str, Any]) -> float:
    """The upstream-reported USD cost, or 0.0 when it reported none.

    Mirrors ``_resolve_usd_cost``'s priority and shares ``coerce_rate``, so
    this helper and the engine agree on *whether* a cost was reported; only
    the arithmetic below is re-derived independently.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    cost_details = usage.get("cost_details")
    if isinstance(cost_details, dict):
        total = coerce_rate(cost_details.get("total_cost"))
        if total is not None and total > 0:
            return total
    for source in (usage, payload):
        for field in ("total_cost", "cost"):
            value = coerce_rate(source.get(field))
            if value is not None and value > 0:
                return value
    return 0.0


def _expected_token_msats(sats_pricing: Any, usage: Any) -> tuple[int, int, int]:
    """Re-derive the token-priced charge independently of the engine.

    Reproduces ``_calculate_from_tokens``'s arithmetic rather than calling the
    engine and comparing it to itself, so a swapped rate, a dropped cache term
    or a changed rounding rule shows up as a mismatch.

    Returns ``(total_msats, input_msats, output_msats)``. Raises ``ValueError``
    on a non-finite rate, which would otherwise crash ``math.ceil`` downstream.
    """
    input_rate = float(sats_pricing.prompt) * 1_000_000.0
    output_rate = float(sats_pricing.completion) * 1_000_000.0
    cache_read_rate = (
        float(sats_pricing.input_cache_read or 0.0) * 1_000_000.0 or input_rate
    )
    cache_write_rate = (
        float(sats_pricing.input_cache_write or 0.0) * 1_000_000.0 or input_rate
    )

    rates = (input_rate, output_rate, cache_read_rate, cache_write_rate)
    if not all(math.isfinite(rate) for rate in rates):
        raise ValueError(f"non-finite pricing rate in {rates!r}")

    calc_input = round(usage.input_tokens / 1000 * input_rate, 3)
    calc_output = round(usage.output_tokens / 1000 * output_rate, 3)
    calc_cache_read = round(usage.cache_read_tokens / 1000 * cache_read_rate, 3)
    calc_cache_write = round(usage.cache_write_tokens / 1000 * cache_write_rate, 3)

    total = math.ceil(calc_input + calc_output + calc_cache_read + calc_cache_write)
    visible_output = int(calc_output)
    return total, total - visible_output, visible_output


def _expected_usd_msats(
    reported_usd: float, provider_fee: float, sats_to_usd: float
) -> int:
    """Re-derive the upstream-reported-USD charge, fee applied then converted."""
    if not all(math.isfinite(x) for x in (reported_usd, provider_fee, sats_to_usd)):
        raise ValueError("non-finite input to the USD charge derivation")
    if sats_to_usd <= 0:
        raise ValueError("sats/USD price must be positive")
    return math.ceil(reported_usd * provider_fee / sats_to_usd * 1000)


def cost_prompt_completion_row(
    *,
    model: "Model",
    probe: ProbeResult,
    cost_data: Any,
    provider_fee: float,
    sats_to_usd: float,
    pricing_known: bool = True,
) -> dict[str, Any]:
    """Check the node's cost engine prices a real completion correctly.

    Both components are checked, since the engine folds the truncated output
    remainder into the input one to keep ``input + output == total``.
    """
    from ..payment.cost_calculation import CostDataError

    payload = probe.chat_payload if isinstance(probe.chat_payload, dict) else {}
    try:
        usage = normalize_usage(payload.get("usage"))
    except Exception:  # noqa: BLE001 - a malformed usage object is a row status
        usage = None
    evidence: dict[str, Any] = {
        "model_id": model.id,
        "forwarded_model_id": model.forwarded_model_id,
        "provider_fee": provider_fee,
        "sats_usd_price": sats_to_usd,
    }

    if isinstance(cost_data, CostDataError):
        evidence["error"] = cost_data.message
        return certification_row(
            "cost.prompt_completion",
            STATUS_FAIL,
            "Prompt and completion cost calculated",
            f"The cost engine could not price the completion: {cost_data.message}.",
            evidence,
        )
    if usage is None:
        return certification_row(
            "cost.prompt_completion",
            STATUS_WARN,
            "Prompt and completion cost calculated",
            "No token usage to price — see the usage row.",
            evidence,
        )
    if model.sats_pricing is None:
        return certification_row(
            "cost.prompt_completion",
            STATUS_WARN,
            "Prompt and completion cost calculated",
            "This model has no computed sats pricing, so there is nothing to "
            "verify the charge against.",
            evidence,
        )
    if not pricing_known:
        return certification_row(
            "cost.prompt_completion",
            STATUS_WARN,
            "Prompt and completion cost calculated",
            "No pricing is known for this model, so the charge cannot be "
            "verified. Configure the model on the node, or pass explicit "
            "prices, to certify this row.",
            evidence,
        )

    reported_usd = _reported_usd_cost(payload)
    try:
        if reported_usd > 0:
            expected_total = _expected_usd_msats(
                reported_usd, provider_fee, sats_to_usd
            )
            expected_input: int | None = None
            expected_output: int | None = None
            basis = "upstream_reported_usd"
        else:
            expected_total, expected_input, expected_output = _expected_token_msats(
                model.sats_pricing, usage
            )
            basis = "configured_token_pricing"
    except (ValueError, OverflowError) as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["reported_usd"] = reported_usd or None
        return certification_row(
            "cost.prompt_completion",
            STATUS_FAIL,
            "Prompt and completion cost calculated",
            f"The expected charge could not be derived from the configured "
            f"pricing: {type(exc).__name__}: {exc}.",
            evidence,
        )

    actual_total = int(cost_data.total_msats)
    actual_input = int(cost_data.input_msats)
    actual_output = int(cost_data.output_msats)

    evidence.update(
        {
            "basis": basis,
            "reported_usd": reported_usd or None,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "expected_total_msats": expected_total,
            "expected_input_msats": expected_input,
            "expected_output_msats": expected_output,
            "actual_total_msats": actual_total,
            "actual_input_msats": actual_input,
            "actual_output_msats": actual_output,
        }
    )

    mismatches: list[str] = []
    if abs(actual_total - expected_total) > COST_TOLERANCE_MSATS:
        mismatches.append(f"total {actual_total} != {expected_total}")
    if actual_input + actual_output != actual_total:
        mismatches.append(
            f"components {actual_input}+{actual_output} != total {actual_total}"
        )
    if (
        expected_output is not None
        and abs(actual_output - expected_output) > COST_TOLERANCE_MSATS
    ):
        mismatches.append(f"output {actual_output} != {expected_output}")
    if (
        expected_input is not None
        and abs(actual_input - expected_input) > COST_TOLERANCE_MSATS
    ):
        mismatches.append(f"input {actual_input} != {expected_input}")

    if mismatches:
        return certification_row(
            "cost.prompt_completion",
            STATUS_FAIL,
            "Prompt and completion cost calculated",
            "The computed charge disagrees with the configured pricing: "
            + "; ".join(mismatches)
            + ".",
            evidence,
        )
    return certification_row(
        "cost.prompt_completion",
        STATUS_OK,
        "Prompt and completion cost calculated",
        f"Charged {actual_total} msats ({actual_input} input + "
        f"{actual_output} output) for {usage.input_tokens} prompt and "
        f"{usage.output_tokens} completion tokens, matching the configured "
        f"pricing.",
        evidence,
    )


async def run_live_checks(
    base_url: str,
    api_key: str,
    model: "Model",
    *,
    provider_fee: float,
    sats_to_usd: float,
    client: httpx.AsyncClient | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    pricing_known: bool = True,
) -> list[dict[str, Any]]:
    """Probe one upstream once and build the five live/derived rows."""
    probe = await probe_upstream(
        base_url,
        api_key,
        model.forwarded_model_id or model.id,
        client=client,
        timeout=timeout,
    )
    rows = [
        safe_row(
            "endpoint.validity",
            "Upstream URL is well-formed",
            lambda: endpoint_validity_row(base_url),
        ),
        safe_row(
            "endpoint.reachable", "Endpoint responds", lambda: heartbeat_row(probe)
        ),
        safe_row(
            "endpoint.models_payload",
            "Models payload has the expected shape",
            lambda: models_payload_row(probe),
        ),
        safe_row(
            "usage.capture",
            "Token usage captured from a completion",
            lambda: usage_capture_row(probe),
        ),
    ]

    cost_data: Any = None
    if probe.chat_payload is not None and probe.chat_status is not None:
        try:
            cost_data = await calculate_cost(
                probe.chat_payload,
                _PROBE_MAX_COST_MSATS,
                model_obj=model,
                provider_fee=provider_fee,
            )
        except Exception as exc:  # noqa: BLE001 - a raising engine is a fail row
            from ..payment.cost_calculation import CostDataError

            cost_data = CostDataError(
                message=f"{type(exc).__name__}: {exc}", code="pricing_error"
            )
    if cost_data is None:
        from ..payment.cost_calculation import CostDataError

        cost_data = CostDataError(
            message=probe.chat_error or "the completion probe did not succeed",
            code="no_completion",
        )

    rows.append(
        safe_row(
            "cost.prompt_completion",
            "Prompt and completion cost calculated",
            lambda: cost_prompt_completion_row(
                model=model,
                probe=probe,
                cost_data=cost_data,
                provider_fee=provider_fee,
                sats_to_usd=sats_to_usd,
                pricing_known=pricing_known,
            ),
        )
    )
    return rows


# The standalone runner certifies a URL before it is configured, so it reads
# nothing from the node's database: the pricing rows do not apply, and the cost
# row falls back to litellm's cost map or explicit prices.


def _first_model_id(probe: ProbeResult) -> str | None:
    data = (probe.models_payload or {}).get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id:
                return model_id
    return None


def _as_price(value: Any) -> float | None:
    """A USD-per-token price from outside the node, or ``None``.

    Shares ``coerce_rate`` so an explicit ``--prompt-price`` is validated
    exactly like a litellm-derived one.
    """
    return coerce_rate(value)


async def _resolve_sats_usd_price(override: float | None) -> float | None:
    """The sats/USD price for a standalone run, or ``None`` if unavailable.

    ``SATS_USD_PRICE`` is a module global populated by the app's lifespan
    background task, so a fresh ``python -m`` process has none and
    ``sats_usd_price()`` raises ``ValueError``. That must not abort a
    certification run: fall back to the BTC global, then try the exchange
    feed once, and return ``None`` rather than raising so the cost row can
    degrade to a ``warn`` and the rest of the report still prints.
    """
    if override is not None:
        return override if math.isfinite(override) and override > 0 else None

    from ..payment import price as price_module

    if price_module.SATS_USD_PRICE:
        return float(price_module.SATS_USD_PRICE)
    if price_module.BTC_USD_PRICE:
        return float(price_module.BTC_USD_PRICE) / price_module.SATS_PER_BTC

    try:
        await price_module._update_prices()
    except Exception as exc:  # noqa: BLE001 - no price is a row status
        logger.warning(
            "Could not initialize the sats/USD price for the standalone run",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )
        return None
    if price_module.SATS_USD_PRICE:
        return float(price_module.SATS_USD_PRICE)
    return None


def _model_from_usd_pricing(
    model_id: str, prompt_usd: float, completion_usd: float, sats_to_usd: float
) -> "Model":
    """A throwaway ``Model`` carrying just enough to exercise the cost engine."""
    from ..payment.models import (
        Architecture,
        Model,
        Pricing,
        _update_model_sats_pricing,
    )

    model = Model(
        id=model_id,
        name=model_id,
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
        pricing=Pricing(prompt=prompt_usd, completion=completion_usd),
        sats_pricing=None,
        per_request_limits=None,
        top_provider=None,
        enabled=True,
        upstream_provider_id=None,
        canonical_slug=None,
    )
    return _update_model_sats_pricing(model, sats_to_usd)


async def certify_upstream_url(
    base_url: str,
    *,
    api_key: str = "",
    model_id: str | None = None,
    prompt_price: float | None = None,
    completion_price: float | None = None,
    provider_fee: float = 1.0,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    sats_usd_price: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Certify an arbitrary upstream URL without touching the node's DB."""
    from ..payment.models import litellm_cost_entry

    sats_to_usd = await _resolve_sats_usd_price(sats_usd_price)
    target: dict[str, Any] = {"base_url": base_url, "model_id": model_id}

    if not model_id:
        discovery = await probe_upstream(
            base_url, api_key, "", client=client, timeout=timeout
        )
        model_id = _first_model_id(discovery)
        target["model_id"] = model_id
        if model_id is None:
            rows = [
                endpoint_validity_row(base_url),
                heartbeat_row(discovery),
                models_payload_row(discovery),
                certification_row(
                    "usage.capture",
                    STATUS_FAIL,
                    "Token usage captured from a completion",
                    "No model id is available to probe: pass --model, or the "
                    "upstream must list at least one id.",
                    {"url": discovery.chat_url},
                ),
                certification_row(
                    "cost.prompt_completion",
                    STATUS_FAIL,
                    "Prompt and completion cost calculated",
                    "No model id is available to price.",
                    {},
                ),
            ]
            return {
                "target": target,
                "rows": rows,
                "checklist": build_checklist(rows),
            }

    entry = litellm_cost_entry(model_id) or {}
    resolved_prompt = (
        _as_price(prompt_price)
        if prompt_price is not None
        else _as_price(entry.get("input_cost_per_token"))
    )
    resolved_completion = (
        _as_price(completion_price)
        if completion_price is not None
        else _as_price(entry.get("output_cost_per_token"))
    )
    pricing_known = (
        resolved_prompt is not None
        and resolved_completion is not None
        and sats_to_usd is not None
    )
    target["prompt_price_usd"] = resolved_prompt
    target["completion_price_usd"] = resolved_completion
    target["sats_usd_price"] = sats_to_usd

    model = _model_from_usd_pricing(
        model_id,
        resolved_prompt or 0.0,
        resolved_completion or 0.0,
        sats_to_usd or 1.0,
    )
    rows = await run_live_checks(
        base_url,
        api_key,
        model,
        provider_fee=provider_fee,
        sats_to_usd=sats_to_usd or 1.0,
        client=client,
        timeout=timeout,
        pricing_known=pricing_known,
    )
    return {"target": target, "rows": rows, "checklist": build_checklist(rows)}


def render_checklist(result: dict[str, Any]) -> str:
    target = result.get("target", {})
    lines = [f"Upstream certification — {target.get('base_url')}"]
    if target.get("model_id"):
        lines.append(f"  model: {target['model_id']}")
    lines.append("")
    lines.append("  checklist")
    for item in result.get("checklist", []):
        lines.append(f"    {item['tick']} {item['label']}")
    lines.append("")
    lines.append("  rows")
    for row in result.get("rows", []):
        tick = TICKS.get(row["status"], "?")
        lines.append(f"    {tick} [{row['id']}] {row['detail']}")
    return "\n".join(lines)


def _route_logs_to_stderr() -> None:
    """Move the app's stdout log handlers to stderr, so log records cannot
    interleave with the report."""
    import logging

    loggers = [logging.getLogger()]
    loggers.extend(
        obj
        for obj in logging.root.manager.loggerDict.values()
        if isinstance(obj, logging.Logger)
    )
    for logger in loggers:
        for handler in list(logger.handlers):
            if (
                isinstance(handler, logging.StreamHandler)
                and getattr(handler, "stream", None) is sys.stdout
            ):
                handler.setStream(sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Run the checklist against one or more upstream base URLs."""
    _route_logs_to_stderr()
    parser = argparse.ArgumentParser(
        prog="python -m routstr.upstream.certification",
        description=(
            "Run the upstream certification checklist against one or more "
            "upstream base URLs. Exits non-zero when any row fails."
        ),
    )
    parser.add_argument(
        "--url",
        action="append",
        required=True,
        help="Upstream base URL (repeatable), e.g. https://api.example.com/v1",
    )
    parser.add_argument("--key", default="", help="Bearer API key for the upstream")
    parser.add_argument(
        "--model",
        default=None,
        help="Model id to probe (defaults to the first id the upstream lists)",
    )
    parser.add_argument(
        "--prompt-price",
        type=float,
        default=None,
        help="USD per prompt token (defaults to litellm's cost map)",
    )
    parser.add_argument(
        "--completion-price",
        type=float,
        default=None,
        help="USD per completion token (defaults to litellm's cost map)",
    )
    parser.add_argument(
        "--provider-fee",
        type=float,
        default=1.0,
        help="Provider fee multiplier applied by the cost check",
    )
    parser.add_argument(
        "--sats-usd-price",
        type=float,
        default=None,
        help=(
            "USD per satoshi for the cost check. Defaults to the node's "
            "live rate, initialized from the exchange feed when unset."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=PROBE_TIMEOUT_SECONDS,
        help="Per-request probe timeout in seconds",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the raw report as JSON"
    )
    parser.add_argument(
        "--json-out",
        default=None,
        metavar="PATH",
        help=(
            "Write the raw JSON report to PATH ('-' for stdout). Unlike "
            "--json, nothing else is written there, so the file is always "
            "parseable — use this in pipelines."
        ),
    )
    args = parser.parse_args(argv)

    async def _run_all() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for url in args.url:
            results.append(
                await certify_upstream_url(
                    url,
                    api_key=args.key,
                    model_id=args.model,
                    prompt_price=args.prompt_price,
                    completion_price=args.completion_price,
                    provider_fee=args.provider_fee,
                    timeout=args.timeout,
                    sats_usd_price=args.sats_usd_price,
                )
            )
        return results

    results = asyncio.run(_run_all())

    if args.json_out is not None:
        document = json.dumps(results, indent=2, default=str)
        if args.json_out == "-":
            print(document)
        else:
            with open(args.json_out, "w", encoding="utf-8") as handle:
                handle.write(document + "\n")

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    elif args.json_out is None:
        for result in results:
            print(render_checklist(result))
            print()

    worst = STATUS_OK
    for result in results:
        for row in result.get("rows", []):
            if row["status"] == STATUS_FAIL:
                worst = STATUS_FAIL
            elif row["status"] == STATUS_WARN and worst == STATUS_OK:
                worst = STATUS_WARN
    return 1 if worst == STATUS_FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
