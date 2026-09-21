"""Certification checks for an upstream provider endpoint.

PR #717 established the row contract — ``{id, status, title, detail,
evidence}`` with ``status`` in ``{ok, warn, fail}`` — and the four pricing
rows derived from the database row plus the in-process served map. Those
rows deliberately never touch the network. This module adds the checks that
*must* touch the network, and the checklist view that maps the
operator-facing goals onto rows:

=========================  =========================================
Goal                       Row(s)
=========================  =========================================
Heartbeat                  ``endpoint.reachable``
Usage data                 ``usage.capture``
Cost data                  ``cost.prompt_completion``
Pricing in ``/v1/models``  ``pricing.served_matches_configured``,
                           ``pricing.enabled_models_served``
=========================  =========================================

**Money safety.** Every live check calls the upstream directly with
``httpx`` — exactly like the existing ``POST /api/models/test`` probe — and
never enters the node's billing path. No reservation is taken, no Cashu
token is minted or spent, and the probe asks for a single token
(``max_tokens=1``). A probe therefore costs the operator at most one
completion's worth of upstream spend and nothing from the node's wallet.

**Why a separate endpoint.** ``GET …/report`` promises the operator a
cheap, non-blocking read. A live probe can hang for the length of its
timeout and spends upstream credit, so it lives behind
``POST …/certify`` instead of being folded into the read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from ..core.logging import get_logger
from ..payment.cost_calculation import calculate_cost
from ..payment.usage import normalize_usage

if TYPE_CHECKING:
    from ..payment.models import Model

logger = get_logger(__name__)

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

TICKS = {STATUS_OK: "☑️", STATUS_WARN: "⚠️", STATUS_FAIL: "❌"}

# A probe must never be able to wedge an admin request. Fifteen seconds is
# generous for a `/models` listing or a one-token completion on a healthy
# upstream, and bounded enough that a dead host fails the row rather than
# the request.
PROBE_TIMEOUT_SECONDS = 15.0

# The cheapest request that still exercises the usage/cost path: one token
# out. Anything larger only spends more upstream credit for no extra
# signal.
PROBE_MAX_TOKENS = 1
PROBE_PROMPT = "ping"

# The reservation ceiling is irrelevant to the token-priced path — it is
# only the amount held before settlement — but ``calculate_cost`` requires
# one. Any value at or above the real charge behaves identically.
_PROBE_MAX_COST_MSATS = 1_000_000_000

# Rounding in ``_calculate_from_tokens`` truncates the output component and
# folds the remainder into the input component, so a one-millisatoshi
# difference is arithmetic, not drift.
COST_TOLERANCE_MSATS = 1


def certification_row(
    row_id: str,
    status: str,
    title: str,
    detail: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one row of the certification report."""
    return {
        "id": row_id,
        "status": status,
        "title": title,
        "detail": detail,
        "evidence": evidence if evidence is not None else {},
    }


# The operator-facing goals, each mapped onto the rows that decide it. A
# goal is ``ok`` only when every row it names is ``ok``; any ``fail`` makes
# it ``fail``; anything else (a ``warn``, or a row that did not run) makes
# it ``warn``. Kept as data so the checklist and the row set cannot drift.
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
    """Summarise the rows as the four operator-facing goals with ticks."""
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


# ---------------------------------------------------------------------------
# Row builders
#
# Every builder below is pure: it turns an already-fetched fact (a probe
# result, a model, a computed cost) into a row. The network lives only in
# ``probe_upstream`` and ``run_live_checks``, so a test can exercise each
# verdict — including the failure ones — without a socket.
# ---------------------------------------------------------------------------


def endpoint_validity_row(base_url: str) -> dict[str, Any]:
    """Check the configured base URL is a well-formed http(s) endpoint."""
    parsed = urlparse(base_url or "")
    problems: list[str] = []
    if parsed.scheme not in ("http", "https"):
        problems.append(f"scheme {parsed.scheme!r} is not http or https")
    if not parsed.netloc:
        problems.append("no host component")
    evidence: dict[str, Any] = {
        "base_url": base_url,
        "scheme": parsed.scheme,
        "host": parsed.netloc,
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
    """Check the upstream's ``/models`` responds — the heartbeat."""
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
    """Check the ``/models`` payload matches the OpenAI list shape."""
    if probe.models_payload is None:
        return certification_row(
            "endpoint.models_payload",
            STATUS_FAIL,
            "Models payload has the expected shape",
            f"Could not read a JSON object from {probe.models_url}: "
            f"{probe.models_error}.",
            {"url": probe.models_url, "error": probe.models_error},
        )

    data = probe.models_payload.get("data")
    if not isinstance(data, list):
        return certification_row(
            "endpoint.models_payload",
            STATUS_FAIL,
            "Models payload has the expected shape",
            f'Expected a top-level "data" list, got {type(data).__name__}.',
            {
                "url": probe.models_url,
                "top_level_keys": sorted(probe.models_payload.keys()),
            },
        )

    ids = [
        item.get("id")
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
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
            f'The "data" list carries no entry with a string "id" '
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

    A missing ``usage`` object is the root of the ``(0+0)`` billing bug —
    the node has nothing to price, so the request settles for free. That is
    a real defect in the upstream's OpenAI compatibility, but it does not
    make the endpoint unusable, so it is a ``warn`` rather than a ``fail``.
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
    if probe.chat_payload is None:
        evidence["error"] = probe.chat_error
        return certification_row(
            "usage.capture",
            STATUS_FAIL,
            "Token usage captured from a completion",
            f"The completion body was not a JSON object: {probe.chat_error}.",
            evidence,
        )

    raw_usage = probe.chat_payload.get("usage")
    normalized = normalize_usage(raw_usage)
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

    Mirrors ``_resolve_usd_cost``'s priority (``cost_details.total_cost``
    then ``total_cost`` then ``cost``) so this check knows which branch of
    the engine it is verifying. It is written out here rather than imported
    on purpose: the point of the cost row is an independent re-derivation,
    and reusing the engine's own helper would make a wrong priority
    self-consistent and therefore invisible.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    cost_details = usage.get("cost_details")
    if isinstance(cost_details, dict):
        total = cost_details.get("total_cost")
        if isinstance(total, (int, float)) and math.isfinite(total) and total > 0:
            return float(total)
    for source in (usage, payload):
        for field in ("total_cost", "cost"):
            value = source.get(field)
            if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                return float(value)
    return 0.0


def _expected_token_msats(sats_pricing: Any, usage: Any) -> tuple[int, int, int]:
    """Re-derive the token-priced charge independently of the engine.

    ``_calculate_from_tokens`` prices at *msats per 1000 tokens*, rounds
    each component to three decimals, ceilings the sum, then folds the
    cache cost into the input component by truncating the output one. The
    arithmetic is reproduced here — rather than calling the engine and
    comparing it to itself — so a swapped input/output rate, a dropped
    cache term or a changed rounding rule shows up as a mismatch.

    Returns ``(total_msats, input_msats, output_msats)``.
    """
    input_rate = float(sats_pricing.prompt) * 1_000_000.0
    output_rate = float(sats_pricing.completion) * 1_000_000.0
    cache_read_rate = (
        float(sats_pricing.input_cache_read or 0.0) * 1_000_000.0 or input_rate
    )
    cache_write_rate = (
        float(sats_pricing.input_cache_write or 0.0) * 1_000_000.0 or input_rate
    )

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

    Both the prompt and the completion component are checked: the engine
    truncates the output component and folds the remainder into the input
    component so that ``input + output == total`` exactly, which means a
    wrong rate on *either* side shows up as a mismatch here.
    """
    from ..payment.cost_calculation import CostDataError

    payload = probe.chat_payload or {}
    usage = normalize_usage(payload.get("usage"))
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
    if reported_usd > 0:
        expected_total = _expected_usd_msats(reported_usd, provider_fee, sats_to_usd)
        expected_input: int | None = None
        expected_output: int | None = None
        basis = "upstream_reported_usd"
    else:
        expected_total, expected_input, expected_output = _expected_token_msats(
            model.sats_pricing, usage
        )
        basis = "configured_token_pricing"

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
        endpoint_validity_row(base_url),
        heartbeat_row(probe),
        models_payload_row(probe),
        usage_capture_row(probe),
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
        cost_prompt_completion_row(
            model=model,
            probe=probe,
            cost_data=cost_data,
            provider_fee=provider_fee,
            sats_to_usd=sats_to_usd,
            pricing_known=pricing_known,
        )
    )
    return rows


# ---------------------------------------------------------------------------
# Standalone runner
#
# ``certify_upstream_url`` deliberately reads nothing from the node's
# database: the point of the CLI is to certify a URL *before* it is
# configured, or one the operator does not want to write into the node at
# all. The four pricing rows therefore do not apply here — they compare a
# stored row against the served map, neither of which exists for a bare
# URL — and the cost row falls back to litellm's cost map (or explicit
# prices) instead of a configured row.
# ---------------------------------------------------------------------------


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
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return float(value)
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
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Certify an arbitrary upstream URL without touching the node's DB."""
    from ..payment.models import litellm_cost_entry
    from ..payment.price import sats_usd_price

    sats_to_usd = sats_usd_price()
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
        prompt_price
        if prompt_price is not None
        else _as_price(entry.get("input_cost_per_token"))
    )
    resolved_completion = (
        completion_price
        if completion_price is not None
        else _as_price(entry.get("output_cost_per_token"))
    )
    pricing_known = resolved_prompt is not None and resolved_completion is not None
    target["prompt_price_usd"] = resolved_prompt
    target["completion_price_usd"] = resolved_completion

    model = _model_from_usd_pricing(
        model_id, resolved_prompt or 0.0, resolved_completion or 0.0, sats_to_usd
    )
    rows = await run_live_checks(
        base_url,
        api_key,
        model,
        provider_fee=provider_fee,
        sats_to_usd=sats_to_usd,
        client=client,
        timeout=timeout,
        pricing_known=pricing_known,
    )
    return {"target": target, "rows": rows, "checklist": build_checklist(rows)}


def render_checklist(result: dict[str, Any]) -> str:
    """Render one certification result as the operator-facing checklist."""
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


def main(argv: list[str] | None = None) -> int:
    """Run the checklist against one or more upstream base URLs."""
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
        "--timeout",
        type=float,
        default=PROBE_TIMEOUT_SECONDS,
        help="Per-request probe timeout in seconds",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the raw report as JSON"
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
                )
            )
        return results

    results = asyncio.run(_run_all())

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
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
