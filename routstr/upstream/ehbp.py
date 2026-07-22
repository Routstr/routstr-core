from __future__ import annotations

import json
import math
import time
import traceback
from dataclasses import dataclass, field
from typing import AsyncIterator, Mapping
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import case
from sqlmodel import col, update

from ..auth import (
    ROUTSTR_FEE_PERCENT,
    ReservationSnapshot,
    _claim_reservation_for_charge,
    _validate_reservation_snapshot,
    get_billing_key,
    get_reservation_snapshot,
    payments_logger,
)
from ..core import get_logger
from ..core.db import (
    ApiKey,
    AsyncSession,
    accumulate_routstr_fee,
)
from ..core.db import (
    store_cashu_transaction_with_retry as store_cashu_transaction,
)
from ..core.exceptions import UpstreamError
from ..core.settings import settings
from ..payment.cost_calculation import (
    CostData,
    MaxCostData,
    calculate_cost,
)
from ..payment.helpers import create_error_response
from ..payment.models import Model
from ..wallet import recieve_token, send_token
from .tinfoil_trailer import forward_with_trailer

logger = get_logger(__name__)

# Provider-neutral confidential-inference defaults.  Tinfoil is the first
# EHBP implementation, but provider-specific routing, usage extraction and
# header policy belong in a profile so future TEE providers do not inherit
# Tinfoil-only assumptions.
_ENCLAVE_URL_HEADER = "X-Tinfoil-Enclave-Url"
_REQUEST_USAGE_HEADER = "X-Tinfoil-Request-Usage-Metrics"
_RESPONSE_USAGE_HEADER = "X-Tinfoil-Usage-Metrics"
_TINFOIL_PROVIDER_TYPE = "tinfoil"
_TINFOIL_ALLOWED_ENCLAVE_HOST_SUFFIX = ".tinfoil.sh"
_TINFOIL_ALLOWED_ENCLAVE_HOSTS = frozenset({"tinfoil.sh"})


def _normalize_upstream_model_id(model_id: str | None) -> str:
    """Normalize casing and whitespace for upstream identity comparisons."""
    if not model_id:
        return ""
    return model_id.strip().lower()


# Headers that must not be forwarded to the upstream enclave.
_PROXY_ONLY_HEADERS = frozenset(
    {
        "x-routstr-model",
        "x-tinfoil-enclave-url",
        "x-tinfoil-request-usage-metrics",
    }
)


def parse_tinfoil_usage_metrics(header_value: str | None) -> dict | None:
    """Parse ``X-Tinfoil-Usage-Metrics`` into an OpenAI-style usage dict.

    The header format is::

        prompt=<n>,completion=<n>,total=<n>[,model=<name>]

    The ``model`` field (added in tinfoilsh/confidential-model-router PR #385)
    is extracted as a string and included in the returned dict under the
    ``"model"`` key so callers can compare the served model against the
    requested one and adjust pricing.

    Returns a dict like ``{"prompt_tokens": n, "completion_tokens": n,
    "model": "<name>"}`` suitable for :func:`calculate_cost` (which ignores
    the extra ``model`` key in the usage sub-dict), or ``None`` when the
    header is absent or malformed.
    """
    if not header_value:
        return None
    parts: dict[str, int] = {}
    model: str | None = None
    for item in header_value.split(","):
        key, sep, value = item.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key == "model":
            model = value
            continue
        try:
            parts[key] = int(value)
        except (ValueError, TypeError):
            continue
    prompt = parts.get("prompt")
    completion = parts.get("completion")
    if prompt is not None and completion is not None:
        result: dict[str, int | str] = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
        }
        if "total" in parts:
            result["total_tokens"] = parts["total"]
        if model:
            result["model"] = model
        return result
    logger.warning(
        "Failed to parse X-Tinfoil-Usage-Metrics header",
        extra={
            "header_value": header_value,
            "parsed_parts": parts,
        },
    )
    return None


def _get_header_case_insensitive(
    headers: Mapping[str, str], header_name: str
) -> str | None:
    header_name_lower = header_name.lower()
    for key, value in headers.items():
        if key.lower() == header_name_lower:
            return value
    return None


def _validated_tinfoil_enclave_base_url(enclave_url: str) -> str | None:
    """Validate and normalize a Tinfoil enclave base URL.

    ``X-Tinfoil-Enclave-Url`` is client supplied. Treating it as an arbitrary
    forwarding destination would let callers turn Routstr into an SSRF proxy and
    exfiltrate upstream Authorization headers. Only HTTPS URLs on Tinfoil-owned
    hostnames are accepted.
    """
    try:
        parsed = urlsplit(enclave_url.strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None

    hostname = parsed.hostname
    if not hostname:
        return None

    host = hostname.rstrip(".").lower()
    if parsed.scheme.lower() != "https":
        return None
    if parsed.username or parsed.password:
        return None
    if port not in (None, 443):
        return None
    if host not in _TINFOIL_ALLOWED_ENCLAVE_HOSTS and not host.endswith(
        _TINFOIL_ALLOWED_ENCLAVE_HOST_SUFFIX
    ):
        return None

    # Preserve an optional base path but discard query/fragment. The request
    # query string is forwarded separately via ``prepare_params``.
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit(("https", netloc, parsed.path.rstrip("/"), "", ""))


def _resolve_ehbp_target_url(
    target_url: str,
    path: str,
    headers: Mapping[str, str],
    provider_type: str | None = None,
    profile: "ConfidentialInferenceProfile | None" = None,
) -> str:
    """Resolve the provider-approved destination for an EHBP request.

    Tinfoil can send the actual enclave URL in ``X-Tinfoil-Enclave-Url`` when
    the SDK is pointed at a Routstr proxy.  A provider profile must explicitly
    opt in to client-supplied target overrides and constrain the destination;
    otherwise the header is ignored so callers cannot redirect other providers
    or leak upstream API keys.
    """
    override_header = profile.client_target_url_header if profile else _ENCLAVE_URL_HEADER
    if not override_header:
        return target_url
    enclave_url = _get_header_case_insensitive(headers, override_header)
    if not enclave_url:
        return target_url

    if profile is None:
        if provider_type != _TINFOIL_PROVIDER_TYPE:
            logger.warning(
                "Ignoring EHBP target override for provider without profile",
                extra={"provider": provider_type or "unknown"},
            )
            return target_url
        validated_base_url = _validated_tinfoil_enclave_base_url(enclave_url)
    elif not profile.allow_client_target_override:
        logger.warning(
            "Ignoring EHBP target override for provider profile",
            extra={"provider": provider_type or "unknown"},
        )
        return target_url
    else:
        validated_base_url = _validated_confidential_target_url(enclave_url, profile)

    if validated_base_url is None:
        logger.warning(
            "Rejected invalid EHBP target override",
            extra={"provider": provider_type or "unknown"},
        )
        raise UpstreamError(
            f"Invalid {override_header}: target is not allowed for this provider",
            status_code=400,
        )

    return f"{validated_base_url}/{path.lstrip('/')}"


def _validated_confidential_target_url(
    enclave_url: str, profile: "ConfidentialInferenceProfile"
) -> str | None:
    # Client target overrides are Tinfoil-only for now. Future confidential
    # inference providers must add their own constrained validator here before
    # opting into ``allow_client_target_override``.
    if profile.client_target_url_header == _ENCLAVE_URL_HEADER:
        return _validated_tinfoil_enclave_base_url(enclave_url)
    return None


def _strip_proxy_headers(
    headers: dict[str, str],
    profile: "ConfidentialInferenceProfile | None" = None,
) -> dict[str, str]:
    """Remove proxy-routing headers that must not reach the upstream enclave."""
    proxy_only_headers = profile.proxy_only_headers if profile else _PROXY_ONLY_HEADERS
    clean = {}
    for key, value in headers.items():
        if key.lower() not in proxy_only_headers:
            clean[key] = value
    return clean


def _prepare_ehbp_upstream_headers(
    headers: dict[str, str],
    target_headers: Mapping[str, str],
    profile: "ConfidentialInferenceProfile | None" = None,
) -> dict[str, str]:
    """Merge safe request headers with provider-controlled EHBP target headers.

    Client-supplied proxy control headers must be stripped, but provider-added
    target headers such as ``X-Tinfoil-Request-Usage-Metrics: true`` must still
    reach the upstream enclave. Strip first, then merge target headers so
    callers cannot spoof proxy controls while providers can opt into protocol
    features.
    """
    return {**_strip_proxy_headers(headers, profile), **dict(target_headers)}


def _build_cost_info(
    total_msats: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    input_msats: int = 0,
    output_msats: int = 0,
    actual_model: str | None = None,
) -> dict:
    """Build a cost-info dict with token counts and per-token-type costs.

    When ``actual_model`` is set (the served model differs from the requested
    one), it is included in the returned dict so callers can use it for billing
    finalization and logging.
    """
    result: dict[str, int | str | None] = {
        "total_msats": total_msats,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_msats": input_msats,
        "output_msats": output_msats,
    }
    if actual_model:
        result["actual_model"] = actual_model
    return result


def _inject_cost_response_headers(
    headers: dict[str, str], cost_info: dict
) -> None:
    """Add per-request cost headers to an EHBP response.

    Since EHBP response bodies are opaque encrypted blobs, cost cannot be
    injected into the JSON body. Instead, it goes into response headers that
    the client/Tinfoil SDK can read without decrypting.
    """
    headers["X-Routstr-Cost-Msats"] = str(cost_info["total_msats"])
    headers["X-Routstr-Input-Cost-Msats"] = str(cost_info["input_msats"])
    headers["X-Routstr-Output-Cost-Msats"] = str(cost_info["output_msats"])


async def _compute_ehbp_actual_cost(
    usage_header: str | None,
    model_obj: Model,
    max_cost_for_model: int,
) -> dict:
    """Compute the actual cost in msats from Tinfoil usage metrics.

    Falls back to ``max_cost_for_model`` when usage is absent (streaming) or
    cannot be priced. The result is clamped to ``[min_request_msat,
    max_cost_for_model]`` so the refund never exceeds the reservation and is
    never zero.

    When the usage-metrics header includes ``model=<name>`` and it differs
    from ``model_obj.id``, the actual served model's pricing is used for the
    cost calculation. The returned dict includes an ``"actual_model"`` key
    in that case so callers can use it for billing finalization.

    Returns a dict with ``total_msats``, ``input_tokens``, ``output_tokens``,
    ``total_tokens``, ``input_msats``, and ``output_msats`` (and optionally
    ``actual_model``).
    """
    usage_dict = parse_tinfoil_usage_metrics(usage_header)
    if usage_dict is None:
        return _build_cost_info(max_cost_for_model)

    # The enclave may serve a different model than the one requested (e.g.
    # due to failover).  The usage-metrics header's ``model=<name>`` carries
    # the actual upstream model ID (e.g. ``glm-5-2``), which may differ from
    # the client-facing ``model_obj.id`` (e.g. ``tinfoil-glm-5-2``) even when
    # the correct model was served — the alias is resolved through
    # ``model_obj.forwarded_model_id``.  Only when the served model differs
    # from the expected upstream ID do we treat it as a real mismatch and
    # look up the actual model's pricing.
    actual_model: str | None = usage_dict.pop("model", None)  # type: ignore[arg-type]
    pricing_model_id = model_obj.id
    expected_upstream_model = model_obj.forwarded_model_id or model_obj.id
    expected_identity = _normalize_upstream_model_id(expected_upstream_model)
    served_identity = _normalize_upstream_model_id(actual_model)

    # Ignore casing and surrounding whitespace when comparing the model
    # reported by the enclave with the expected upstream model. Version
    # suffixes remain part of the identity because a configured
    # ``forwarded_model_id`` may intentionally include one.
    if actual_model and served_identity != expected_identity:
        from ..proxy import get_model_instance

        # ``forwarded_model_id`` values are registered as routable aliases in
        # the global model map. The resolved object can belong to a different
        # provider and therefore have a different client-facing ``id`` while
        # still representing the same upstream model.
        actual_model_obj = get_model_instance(actual_model)
        if actual_model_obj is None:
            logger.warning(
                "EHBP served model not found in registry, falling back "
                "to requested model for pricing",
                extra={
                    "requested_model": model_obj.id,
                    "expected_upstream_model": expected_upstream_model,
                    "actual_model": actual_model,
                },
            )
            actual_model = None
        else:
            resolved_upstream_model = (
                actual_model_obj.forwarded_model_id or actual_model_obj.id
            )
            resolved_identity = _normalize_upstream_model_id(
                resolved_upstream_model
            )
            if resolved_identity != expected_identity:
                logger.info(
                    "EHBP served model differs from requested, using actual "
                    "model for pricing",
                    extra={
                        "requested_model": model_obj.id,
                        "expected_upstream_model": expected_upstream_model,
                        "actual_model": actual_model,
                        "resolved_upstream_model": resolved_upstream_model,
                    },
                )
                pricing_model_id = actual_model_obj.id
            else:
                # A different registry/client alias resolved to the same
                # upstream model; retain the requested model's pricing.
                actual_model = None
    else:
        # Models match or no model in header — use requested model's pricing.
        actual_model = None

    try:
        cost = await calculate_cost(
            {"model": pricing_model_id, "usage": usage_dict},
            max_cost_for_model,
        )
    except Exception as e:
        logger.warning(
            "EHBP usage cost calculation failed, falling back to max cost",
            extra={
                "model": pricing_model_id,
                "error": str(e),
                "usage": usage_dict,
            },
        )
        return _build_cost_info(max_cost_for_model, actual_model=actual_model)

    if isinstance(cost, MaxCostData):
        logger.warning(
            "EHBP calculate_cost returned MaxCostData (no model pricing), "
            "falling back to max cost",
            extra={
                "model": pricing_model_id,
                "max_cost_for_model": max_cost_for_model,
                "usage": usage_dict,
                "cost_total_msats": cost.total_msats,
            },
        )
        return _build_cost_info(max_cost_for_model, actual_model=actual_model)
    if isinstance(cost, CostData):
        actual = max(int(cost.total_msats), int(settings.min_request_msat))
        clamped = min(actual, max_cost_for_model)
        logger.info(
            "EHBP actual cost computed from usage metrics",
            extra={
                "model": pricing_model_id,
                "usage": usage_dict,
                "cost_total_msats": cost.total_msats,
                "clamped_msats": clamped,
                "max_cost_for_model": max_cost_for_model,
            },
        )
        return _build_cost_info(
            total_msats=clamped,
            input_tokens=cost.input_tokens,
            output_tokens=cost.output_tokens,
            input_msats=cost.input_msats,
            output_msats=cost.output_msats,
            actual_model=actual_model,
        )
    # CostDataError
    logger.warning(
        "EHBP usage cost calculation error, falling back to max cost",
        extra={
            "model": pricing_model_id,
            "error": getattr(cost, "message", str(cost)),
        },
    )
    return _build_cost_info(max_cost_for_model, actual_model=actual_model)


def _extract_usage_from_response(
    resp_headers: list[tuple[str, str]],
    trailers: list[tuple[str, str]],
    usage_header_name: str | None = _RESPONSE_USAGE_HEADER,
) -> str | None:
    """Find provider usage metrics in response headers or trailers.

    Non-streaming responses put usage in a response header. Streaming responses
    put it in an HTTP trailer. httpx/httpcore silently discard trailers, so we
    use h11 directly when forwarding EHBP requests.
    """
    if not usage_header_name:
        return None
    usage_header_name_lower = usage_header_name.lower()
    for k, v in resp_headers:
        if k.lower() == usage_header_name_lower:
            return v
    for k, v in trailers:
        if k.lower() == usage_header_name_lower:
            return v
    return None


@dataclass(frozen=True)
class ConfidentialInferenceProfile:
    """Provider-neutral policy for encrypted/confidential inference forwarding."""

    usage_response_header: str | None = None
    client_target_url_header: str | None = None
    allow_client_target_override: bool = False
    proxy_only_headers: frozenset[str] = _PROXY_ONLY_HEADERS


@dataclass(frozen=True)
class EHBPForwardingTarget:
    """Provider-specific destination for an EHBP opaque request."""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    profile: ConfidentialInferenceProfile | None = None


async def finalize_ehbp_actual_cost_payment(
    key: ApiKey,
    session: AsyncSession,
    reserved_cost_for_model: int,
    model_id: str,
    cost_info: dict,
    reservation_snapshot: ReservationSnapshot | None = None,
) -> None:
    """Finalize an EHBP bearer request using clamped provider usage metrics."""
    reservation = reservation_snapshot or await get_reservation_snapshot(key, session)
    await _validate_reservation_snapshot(key, reservation, session)
    if not await _claim_reservation_for_charge(reservation, session):
        return
    reserved_cost_for_model = reservation.reserved_msats
    billing_key = await get_billing_key(key, session)
    key_hash = key.hashed_key
    billing_key_hash = billing_key.hashed_key
    total_cost_msats = max(0, int(cost_info.get("total_msats", reserved_cost_for_model)))
    now = int(time.time())

    safe_reserved = case(
        (
            col(ApiKey.reserved_balance) >= reserved_cost_for_model,
            col(ApiKey.reserved_balance) - reserved_cost_for_model,
        ),
        else_=0,
    )
    cleared_reserved_at = case(
        (
            col(ApiKey.reserved_balance) - reserved_cost_for_model > 0,
            col(ApiKey.reserved_at),
        ),
        else_=None,
    )

    stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
        .values(
            reserved_balance=safe_reserved,
            reserved_at=cleared_reserved_at,
            balance=col(ApiKey.balance) - total_cost_msats,
            total_spent=col(ApiKey.total_spent) + total_cost_msats,
        )
    )
    result = await session.exec(stmt)  # type: ignore[call-overload]

    child_result = None
    if billing_key.hashed_key != key.hashed_key:
        child_stmt = (
            update(ApiKey)
            .where(col(ApiKey.hashed_key) == key.hashed_key)
            .values(
                reserved_balance=safe_reserved,
                reserved_at=cleared_reserved_at,
                total_spent=col(ApiKey.total_spent) + total_cost_msats,
            )
        )
        child_result = await session.exec(child_stmt)  # type: ignore[call-overload]

    if result.rowcount == 0 or (child_result is not None and child_result.rowcount == 0):
        await session.rollback()
        logger.error(
            "Failed to finalize EHBP usage-based payment",
            extra={
                "key_hash": key_hash[:8] + "...",
                "billing_key_hash": billing_key_hash[:8] + "...",
                "model": model_id,
                "reserved_cost_for_model": reserved_cost_for_model,
                "total_cost_msats": total_cost_msats,
                "parent_rowcount": result.rowcount,
                "child_rowcount": getattr(child_result, "rowcount", None),
            },
        )
        return

    await session.commit()
    await session.refresh(billing_key)
    if billing_key.hashed_key != key.hashed_key:
        await session.refresh(key)

    if total_cost_msats > 0 and ROUTSTR_FEE_PERCENT > 0:
        fee_msats = math.ceil(total_cost_msats * ROUTSTR_FEE_PERCENT / 100)
        try:
            await accumulate_routstr_fee(session, fee_msats)
        except Exception as e:
            logger.warning(
                "Failed to accumulate Routstr fee for EHBP request",
                extra={"error": str(e), "fee_msats": fee_msats},
            )

    payments_logger.info(
        "FINALIZE",
        extra={
            "event": "finalize",
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "model": model_id,
            "cost_reserved": reserved_cost_for_model,
            "cost_charged": total_cost_msats,
            "input_tokens": cost_info.get("input_tokens", 0),
            "output_tokens": cost_info.get("output_tokens", 0),
            "balance": billing_key.balance,
            "reserved_balance": billing_key.reserved_balance,
            "total_spent": billing_key.total_spent,
            "finalize_type": "ehbp_usage",
            "finalized_at": now,
        },
    )


async def finalize_ehbp_max_cost_payment(
    key: ApiKey,
    session: AsyncSession,
    max_cost_for_model: int,
    model_id: str,
    reservation_snapshot: ReservationSnapshot | None = None,
) -> None:
    """Finalize an EHBP bearer request by charging the reserved max cost.

    EHBP responses are encrypted, so Routstr cannot inspect token usage. Unlike
    normal completion handlers, this intentionally charges the pre-reserved max
    cost and releases the reservation.
    """
    reservation = reservation_snapshot or await get_reservation_snapshot(key, session)
    await _validate_reservation_snapshot(key, reservation, session)
    if not await _claim_reservation_for_charge(reservation, session):
        return
    max_cost_for_model = reservation.reserved_msats
    billing_key = await get_billing_key(key, session)
    key_hash = key.hashed_key
    billing_key_hash = billing_key.hashed_key
    total_cost_msats = max(0, int(max_cost_for_model))
    now = int(time.time())

    cleared_reserved_at = case(
        (
            col(ApiKey.reserved_balance) - max_cost_for_model > 0,
            col(ApiKey.reserved_at),
        ),
        else_=None,
    )
    safe_reserved = case(
        (
            col(ApiKey.reserved_balance) >= max_cost_for_model,
            col(ApiKey.reserved_balance) - max_cost_for_model,
        ),
        else_=0,
    )

    stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
        .values(
            reserved_balance=safe_reserved,
            reserved_at=cleared_reserved_at,
            balance=col(ApiKey.balance) - total_cost_msats,
            total_spent=col(ApiKey.total_spent) + total_cost_msats,
        )
    )
    result = await session.exec(stmt)  # type: ignore[call-overload]

    if billing_key.hashed_key != key.hashed_key:
        child_safe_reserved = case(
            (
                col(ApiKey.reserved_balance) >= max_cost_for_model,
                col(ApiKey.reserved_balance) - max_cost_for_model,
            ),
            else_=0,
        )
        child_cleared_reserved_at = case(
            (
                col(ApiKey.reserved_balance) - max_cost_for_model > 0,
                col(ApiKey.reserved_at),
            ),
            else_=None,
        )
        child_stmt = (
            update(ApiKey)
            .where(col(ApiKey.hashed_key) == key.hashed_key)
            .values(
                reserved_balance=child_safe_reserved,
                reserved_at=child_cleared_reserved_at,
                total_spent=col(ApiKey.total_spent) + total_cost_msats,
            )
        )
        child_result = await session.exec(child_stmt)  # type: ignore[call-overload]
    else:
        child_result = None

    if result.rowcount == 0 or (child_result is not None and child_result.rowcount == 0):
        await session.rollback()
        logger.error(
            "Failed to finalize EHBP max-cost payment",
            extra={
                "key_hash": key_hash[:8] + "...",
                "billing_key_hash": billing_key_hash[:8] + "...",
                "model": model_id,
                "max_cost_for_model": max_cost_for_model,
                "parent_rowcount": result.rowcount,
                "child_rowcount": getattr(child_result, "rowcount", None),
            },
        )
        return

    await session.commit()

    await session.refresh(billing_key)
    if billing_key.hashed_key != key.hashed_key:
        await session.refresh(key)

    if total_cost_msats > 0 and ROUTSTR_FEE_PERCENT > 0:
        fee_msats = math.ceil(total_cost_msats * ROUTSTR_FEE_PERCENT / 100)
        try:
            await accumulate_routstr_fee(session, fee_msats)
        except Exception as e:
            logger.warning(
                "Failed to accumulate Routstr fee for EHBP request",
                extra={"error": str(e), "fee_msats": fee_msats},
            )

    payments_logger.info(
        "FINALIZE",
        extra={
            "event": "finalize",
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "model": model_id,
            "cost_reserved": max_cost_for_model,
            "cost_charged": total_cost_msats,
            "input_tokens": 0,
            "output_tokens": 0,
            "balance": billing_key.balance,
            "reserved_balance": billing_key.reserved_balance,
            "total_spent": billing_key.total_spent,
            "finalize_type": "ehbp_max_cost",
            "finalized_at": now,
        },
    )


async def send_cashu_refund(
    amount: int,
    unit: str,
    mint: str | None = None,
    request_id: str | None = None,
) -> str:
    """Create a Cashu refund token and record the outgoing transaction."""
    refund_token = await send_token(amount, unit=unit, mint_url=mint)
    try:
        await store_cashu_transaction(
            token=refund_token,
            amount=amount,
            unit=unit,
            mint_url=mint,
            typ="out",
            request_id=request_id,
        )
    except Exception:
        pass
    return refund_token


def _msats_to_unit_amount(msats: int, unit: str) -> int:
    if unit == "msat":
        return msats
    if unit == "sat":
        return (msats + 999) // 1000
    raise ValueError(f"Invalid unit: {unit}")


async def forward_ehbp_request(
    *,
    request: Request,
    path: str,
    headers: dict,
    request_body: bytes | None,
    upstream: object,
    key: ApiKey,
    max_cost_for_model: int,
    session: AsyncSession,
    model_obj: Model,
    reservation_snapshot: ReservationSnapshot | None = None,
) -> Response | StreamingResponse:
    """Forward an EHBP bearer-auth request and finalize billing.

    Sends ``X-Tinfoil-Request-Usage-Metrics: true`` so the enclave returns token
    counts in the ``X-Tinfoil-Usage-Metrics`` response header (non-streaming) or
    trailer (streaming). Usage is captured from both response headers and HTTP
    trailers via an h11-based client (httpx silently discards trailers).
    """
    target = upstream.get_ehbp_forwarding_target(path, model_obj)  # type: ignore[attr-defined]

    provider_type = getattr(upstream, "provider_type", "unknown")
    profile = target.profile or upstream.get_confidential_inference_profile()  # type: ignore[attr-defined]
    target_url = _resolve_ehbp_target_url(
        target.url, path, headers, provider_type, profile
    )
    upstream_headers = _prepare_ehbp_upstream_headers(headers, target.headers, profile)

    # Merge query params into the target URL since forward_with_trailer
    # doesn't have a separate params argument.
    query_params = upstream.prepare_params(path, request.query_params)  # type: ignore[attr-defined]
    if query_params:
        from urllib.parse import urlencode

        target_url = f"{target_url}?{urlencode(query_params)}"

    logger.debug(
        "Forwarding EHBP request to upstream",
        extra={
            "url": target_url,
            "method": request.method,
            "path": path,
            "model": model_obj.id,
            "provider": provider_type,
            "key_hash": key.hashed_key[:8] + "...",
        },
    )

    try:
        resp = await forward_with_trailer(
            method=request.method,
            url=target_url,
            headers=upstream_headers,
            body=request_body or b"",
        )

        if resp.status_code != 200:
            body_preview = resp.body.decode("utf-8", errors="ignore").strip()[:500]
            logger.error(
                "EHBP upstream %s returned %s for model=%s path=%s: %s",
                provider_type,
                resp.status_code,
                model_obj.id,
                path,
                body_preview or "<empty>",
                extra={
                    "provider": provider_type,
                    "model": model_obj.id,
                    "status_code": resp.status_code,
                    "path": path,
                    "body_preview": body_preview,
                },
            )
            raise UpstreamError(
                f"EHBP upstream {provider_type} returned {resp.status_code} "
                f"for model {model_obj.id}: {body_preview[:200] or '<empty>'}",
                status_code=resp.status_code,
            )

        # Check for usage metrics in response headers (non-streaming) or
        # trailers (streaming). h11 captures both.
        usage_header_name = (
            profile.usage_response_header if profile else _RESPONSE_USAGE_HEADER
        )
        usage_header = _extract_usage_from_response(
            resp.headers, resp.trailers, usage_header_name
        )
        usage_dict = parse_tinfoil_usage_metrics(usage_header)
        usage_source = (
            "header"
            if usage_header_name
            and any(k.lower() == usage_header_name.lower() for k, _ in resp.headers)
            else ("trailer" if usage_header else "none")
        )

        logger.info(
            "EHBP upstream response received",
            extra={
                "model": model_obj.id,
                "provider": provider_type,
                "target_url": target_url,
                "status_code": resp.status_code,
                "usage_header_raw": usage_header,
                "usage_source": usage_source,
                "has_trailers": bool(resp.trailers),
                "body_length": len(resp.body),
                "key_hash": key.hashed_key[:8] + "...",
            },
        )

        if usage_dict is not None:
            logger.info(
                "EHBP usage metrics received, finalizing with actual token cost",
                extra={
                    "model": model_obj.id,
                    "provider": provider_type,
                    "usage": usage_dict,
                    "usage_source": usage_source,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )
            cost_info = await _compute_ehbp_actual_cost(
                usage_header, model_obj, max_cost_for_model
            )
            # Use the actual served model for billing when it differs from
            # the requested model.
            billing_model = cost_info.pop("actual_model", None) or model_obj.id
            await finalize_ehbp_actual_cost_payment(
                key,
                session,
                max_cost_for_model,
                billing_model,
                cost_info,
                reservation_snapshot,
            )
            cost_data = {**cost_info, "total_usd": 0.0}
        else:
            logger.warning(
                "EHBP usage metrics not found in headers or trailers, "
                "falling back to max-cost billing",
                extra={
                    "model": model_obj.id,
                    "provider": provider_type,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )
            await finalize_ehbp_max_cost_payment(
                key,
                session,
                max_cost_for_model,
                model_obj.id,
                reservation_snapshot,
            )
            cost_data = {
                "total_msats": max_cost_for_model,
                "total_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
            }

        # Build the cost_info dict from what adjust_payment_for_tokens returned
        # or from the max-cost fallback. Fields match CostData/MaxCostData.dict().
        cost_info = {
            "total_msats": cost_data.get("total_msats", max_cost_for_model),
            "input_tokens": cost_data.get("input_tokens", 0),
            "output_tokens": cost_data.get("output_tokens", 0),
            "total_tokens": cost_data.get("input_tokens", 0)
            + cost_data.get("output_tokens", 0),
            "input_msats": cost_data.get("input_msats", 0),
            "output_msats": cost_data.get("output_msats", 0),
        }
        cost_usd = cost_data.get("total_usd", 0.0)

        # Build response headers, filtering out hop-by-hop headers
        response_headers: dict[str, str] = {}
        hop_by_hop = {
            "connection",
            "keep-alive",
            "transfer-encoding",
            "trailer",
            "content-length",
        }
        for k, v in resp.headers:
            if k.lower() not in hop_by_hop:
                response_headers[k] = v

        # Surface per-request cost to the client. Since EHBP bodies are
        # opaque, cost info can only go into response headers.
        _inject_cost_response_headers(response_headers, cost_info)
        response_headers["X-Routstr-Cost-Usd"] = str(cost_usd)

        async def _stream_body() -> AsyncIterator[bytes]:
            yield resp.body

        return StreamingResponse(
            _stream_body(),
            status_code=resp.status_code,
            headers=response_headers,
        )
    except UpstreamError:
        raise
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(
            "Unexpected error in EHBP upstream forwarding",
            extra={
                "error": str(exc),
                "error_type": type(exc).__name__,
                "method": request.method,
                "url": target_url,
                "path": path,
                "traceback": tb,
            },
        )
        raise UpstreamError("An unexpected server error occurred", status_code=500)


async def forward_ehbp_x_cashu_request(
    *,
    request: Request,
    x_cashu_token: str,
    path: str,
    max_cost_for_model: int,
    model_obj: Model,
    upstream: object,
) -> Response | StreamingResponse:
    """Redeem X-Cashu, forward EHBP opaquely, and refund unspent value.

    When the upstream returns ``X-Tinfoil-Usage-Metrics`` in the response
    header (non-streaming) or as an HTTP trailer (streaming), the refund is
    computed from the actual token cost. Trailers are captured via an h11-based
    client because httpx silently discards them.
    """
    request_id = getattr(request.state, "request_id", None)
    amount = 0
    unit = "msat"
    mint: str | None = None
    redeemed = False

    try:
        amount, unit, mint = await recieve_token(x_cashu_token)
        redeemed = True
        try:
            await store_cashu_transaction(
                token=x_cashu_token,
                amount=amount,
                unit=unit,
                mint_url=mint,
                typ="in",
                request_id=request_id,
                collected=True,
            )
        except Exception:
            pass

        headers = upstream.prepare_headers(dict(request.headers))  # type: ignore[attr-defined]
        target = upstream.get_ehbp_forwarding_target(path, model_obj)  # type: ignore[attr-defined]
        provider_type = getattr(upstream, "provider_type", "unknown")
        profile = target.profile or upstream.get_confidential_inference_profile()  # type: ignore[attr-defined]
        target_url = _resolve_ehbp_target_url(
            target.url, path, headers, provider_type, profile
        )
        upstream_headers = _prepare_ehbp_upstream_headers(headers, target.headers, profile)
        request_body = await request.body()

        # Merge query params into the target URL
        query_params = upstream.prepare_params(path, request.query_params)  # type: ignore[attr-defined]
        if query_params:
            from urllib.parse import urlencode

            target_url = f"{target_url}?{urlencode(query_params)}"

        try:
            resp = await forward_with_trailer(
                method=request.method,
                url=target_url,
                headers=upstream_headers,
                body=request_body,
            )

            if resp.status_code != 200:
                refund_token = await send_cashu_refund(amount, unit, mint, request_id)
                error_response = Response(
                    content=json.dumps(
                        {
                            "error": {
                                "message": "Error forwarding EHBP request to upstream",
                                "type": "upstream_error",
                                "code": resp.status_code,
                                "refund_token": refund_token,
                            }
                        }
                    ),
                    status_code=resp.status_code,
                    media_type="application/json",
                )
                error_response.headers["X-Cashu"] = refund_token
                return error_response

            # Compute refund from actual usage when available — check both
            # response headers (non-streaming) and trailers (streaming).
            usage_header_name = (
                profile.usage_response_header if profile else _RESPONSE_USAGE_HEADER
            )
            usage_header = _extract_usage_from_response(
                resp.headers, resp.trailers, usage_header_name
            )
            usage_source = (
                "header"
                if usage_header_name
                and any(
                    k.lower() == usage_header_name.lower() for k, _ in resp.headers
                )
                else ("trailer" if usage_header else "none")
            )

            logger.info(
                "EHBP X-Cashu upstream response received",
                extra={
                    "model": model_obj.id,
                    "provider": provider_type,
                    "target_url": target_url,
                    "status_code": resp.status_code,
                    "usage_header_raw": usage_header,
                    "usage_source": usage_source,
                    "has_trailers": bool(resp.trailers),
                    "body_length": len(resp.body),
                    "redeemed_amount": amount,
                    "unit": unit,
                    "max_cost_for_model": max_cost_for_model,
                },
            )

            cost_info = await _compute_ehbp_actual_cost(
                usage_header, model_obj, max_cost_for_model
            )
            actual_cost_msats = cost_info["total_msats"]
            actual_model = cost_info.get("actual_model")
            billing_model = actual_model or model_obj.id
            refund_amount = amount - _msats_to_unit_amount(actual_cost_msats, unit)
            logger.info(
                "EHBP X-Cashu refund computed",
                extra={
                    "model": billing_model,
                    "requested_model": model_obj.id,
                    "actual_model": actual_model,
                    "redeemed_amount": amount,
                    "actual_cost_msats": actual_cost_msats,
                    "refund_amount": refund_amount,
                    "unit": unit,
                    "usage_source": usage_source,
                },
            )

            # Build response headers, filtering out hop-by-hop headers
            response_headers: dict[str, str] = {}
            hop_by_hop = {
                "connection",
                "keep-alive",
                "transfer-encoding",
                "trailer",
                "content-length",
            }
            for k, v in resp.headers:
                if k.lower() not in hop_by_hop:
                    response_headers[k] = v

            # Surface per-request cost to the client. Since EHBP bodies are
            # opaque encrypted blobs, cost can only go into response headers.
            _inject_cost_response_headers(response_headers, cost_info)

            if refund_amount > 0:
                response_headers["X-Cashu"] = await send_cashu_refund(
                    refund_amount, unit, mint, request_id
                )

            async def _stream_body_xcashu() -> AsyncIterator[bytes]:
                yield resp.body

            return StreamingResponse(
                _stream_body_xcashu(),
                status_code=resp.status_code,
                headers=response_headers,
            )
        except Exception:
            raise

    except Exception as e:
        error_message = str(e)
        logger.error(
            "EHBP X-Cashu request failed",
            extra={
                "error": error_message,
                "error_type": type(e).__name__,
                "path": path,
                "method": request.method,
                "redeemed": redeemed,
            },
        )

        if redeemed and amount > 0:
            try:
                refund_token = await send_cashu_refund(amount, unit, mint, request_id)
                error_response = create_error_response(
                    "upstream_error",
                    "EHBP request failed after token redemption; refunded token",
                    502,
                    request=request,
                )
                error_response.headers["X-Cashu"] = refund_token
                return error_response
            except Exception as refund_error:
                logger.error(
                    "Failed to refund EHBP X-Cashu token after error",
                    extra={
                        "error": str(refund_error),
                        "original_error": error_message,
                    },
                )

        if "already spent" in error_message.lower():
            return create_error_response(
                "token_already_spent",
                "The provided CASHU token has already been spent",
                400,
                request=request,
                token=x_cashu_token,
            )

        if "invalid token" in error_message.lower():
            return create_error_response(
                "invalid_token",
                "The provided CASHU token is invalid",
                400,
                request=request,
                token=x_cashu_token,
            )

        if "mint error" in error_message.lower():
            return create_error_response(
                "mint_error",
                f"CASHU mint error: {error_message}",
                422,
                request=request,
                token=x_cashu_token,
            )

        return create_error_response(
            "cashu_error" if not redeemed else "upstream_error",
            f"EHBP X-Cashu request failed: {error_message}",
            400 if not redeemed else 502,
            request=request,
            token=x_cashu_token if not redeemed else None,
        )
