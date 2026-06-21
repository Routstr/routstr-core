from __future__ import annotations

import json
import math
import time
import traceback
from dataclasses import dataclass, field
from typing import Mapping

import httpx
from fastapi import BackgroundTasks, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import case
from sqlmodel import col, update

from ..auth import (
    ROUTSTR_FEE_PERCENT,
    adjust_payment_for_tokens,
    get_billing_key,
    payments_logger,
)
from ..core import get_logger
from ..core.db import (
    ApiKey,
    AsyncSession,
    accumulate_routstr_fee,
    store_cashu_transaction,
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

logger = get_logger(__name__)

# Headers that the Tinfoil SDK sends to tell the proxy where to forward the
# encrypted request, and the request/response usage-metrics pair.
_ENCLAVE_URL_HEADER = "X-Tinfoil-Enclave-Url"
_REQUEST_USAGE_HEADER = "X-Tinfoil-Request-Usage-Metrics"
_RESPONSE_USAGE_HEADER = "X-Tinfoil-Usage-Metrics"

# Headers that must not be forwarded to the upstream enclave.
_PROXY_ONLY_HEADERS = {
    "x-routstr-model",
    "x-tinfoil-enclave-url",
    "x-tinfoil-request-usage-metrics",
}


def parse_tinfoil_usage_metrics(header_value: str | None) -> dict | None:
    """Parse ``X-Tinfoil-Usage-Metrics`` into an OpenAI-style usage dict.

    The header format is ``prompt=<n>,completion=<n>,total=<n>``. Returns a dict
    like ``{"prompt_tokens": n, "completion_tokens": n}`` suitable for
    :func:`calculate_cost`, or ``None`` when the header is absent or malformed.
    """
    if not header_value:
        return None
    parts: dict[str, int] = {}
    for item in header_value.split(","):
        key, sep, value = item.partition("=")
        if not sep:
            continue
        try:
            parts[key.strip()] = int(value.strip())
        except (ValueError, TypeError):
            continue
    prompt = parts.get("prompt")
    completion = parts.get("completion")
    if prompt is not None and completion is not None:
        result: dict[str, int] = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
        }
        if "total" in parts:
            result["total_tokens"] = parts["total"]
        return result
    return None


def _resolve_ehbp_target_url(
    target_url: str, path: str, headers: Mapping[str, str]
) -> str:
    """Override the forwarding URL with ``X-Tinfoil-Enclave-Url`` if present.

    When the Tinfoil SDK is configured with a proxy ``baseURL``, it sends the
    actual enclave URL in ``X-Tinfoil-Enclave-Url``. The proxy must forward to
    that URL, not to its own default, so the encrypted payload reaches the same
    enclave the client verified.
    """
    enclave_url = (
        headers.get(_ENCLAVE_URL_HEADER)
        or headers.get(_ENCLAVE_URL_HEADER.lower())
        or headers.get(_ENCLAVE_URL_HEADER.upper())
    )
    if enclave_url:
        return f"{enclave_url.rstrip('/')}/{path.lstrip('/')}"
    return target_url


def _strip_proxy_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove proxy-routing headers that must not reach the upstream enclave."""
    clean = {}
    for key, value in headers.items():
        if key.lower() not in _PROXY_ONLY_HEADERS:
            clean[key] = value
    return clean


async def _compute_ehbp_actual_cost(
    usage_header: str | None,
    model_obj: Model,
    max_cost_for_model: int,
) -> int:
    """Compute the actual cost in msats from Tinfoil usage metrics.

    Falls back to ``max_cost_for_model`` when usage is absent (streaming) or
    cannot be priced. The result is clamped to ``[min_request_msat,
    max_cost_for_model]`` so the refund never exceeds the reservation and is
    never zero.
    """
    usage_dict = parse_tinfoil_usage_metrics(usage_header)
    if usage_dict is None:
        return max_cost_for_model

    try:
        cost = await calculate_cost(
            {"model": model_obj.id, "usage": usage_dict},
            max_cost_for_model,
        )
    except Exception as e:
        logger.warning(
            "EHBP usage cost calculation failed, falling back to max cost",
            extra={
                "model": model_obj.id,
                "error": str(e),
                "usage": usage_dict,
            },
        )
        return max_cost_for_model

    if isinstance(cost, MaxCostData):
        return max_cost_for_model
    if isinstance(cost, CostData):
        actual = max(int(cost.total_msats), int(settings.min_request_msat))
        return min(actual, max_cost_for_model)
    # CostDataError
    logger.warning(
        "EHBP usage cost calculation error, falling back to max cost",
        extra={
            "model": model_obj.id,
            "error": getattr(cost, "message", str(cost)),
        },
    )
    return max_cost_for_model


@dataclass(frozen=True)
class EHBPForwardingTarget:
    """Provider-specific destination for an EHBP opaque request."""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)


async def finalize_ehbp_max_cost_payment(
    key: ApiKey,
    session: AsyncSession,
    max_cost_for_model: int,
    model_id: str,
) -> None:
    """Finalize an EHBP bearer request by charging the reserved max cost.

    EHBP responses are encrypted, so Routstr cannot inspect token usage. Unlike
    normal completion handlers, this intentionally charges the pre-reserved max
    cost and releases the reservation.
    """
    billing_key = await get_billing_key(key, session)
    total_cost_msats = max(0, int(max_cost_for_model))
    now = int(time.time())

    cleared_reserved_at = case(
        (col(ApiKey.reserved_balance) - max_cost_for_model > 0, col(ApiKey.reserved_at)),
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
        await session.exec(child_stmt)  # type: ignore[call-overload]

    await session.commit()

    if result.rowcount == 0:
        logger.error(
            "Failed to finalize EHBP max-cost payment",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "billing_key_hash": billing_key.hashed_key[:8] + "...",
                "model": model_id,
                "max_cost_for_model": max_cost_for_model,
            },
        )
        return

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
) -> Response | StreamingResponse:
    """Forward an EHBP bearer-auth request and finalize billing.

    Sends ``X-Tinfoil-Request-Usage-Metrics: true`` so the enclave returns token
    counts in the ``X-Tinfoil-Usage-Metrics`` response header (non-streaming) or
    trailer (streaming). When usage is available in the response header,
    billing is finalized to the actual token cost via
    :func:`adjust_payment_for_tokens`. When usage is not available (streaming
    or unsupported upstream), billing falls back to max-cost.
    """
    target = upstream.get_ehbp_forwarding_target(path, model_obj)  # type: ignore[attr-defined]
    target_url = _resolve_ehbp_target_url(target.url, path, headers)
    upstream_headers = _strip_proxy_headers({**headers, **dict(target.headers)})

    provider_type = getattr(upstream, "provider_type", "unknown")
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

    client = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=1),
        timeout=None,
    )

    try:
        response = await client.send(
            client.build_request(
                request.method,
                target_url,
                headers=upstream_headers,
                content=request_body,
                params=upstream.prepare_params(path, request.query_params),  # type: ignore[attr-defined]
            ),
            stream=True,
        )

        if response.status_code != 200:
            body_bytes = await response.aread()
            body_preview = body_bytes.decode("utf-8", errors="ignore").strip()[:500]
            logger.error(
                "EHBP upstream %s returned %s for model=%s path=%s: %s",
                provider_type,
                response.status_code,
                model_obj.id,
                path,
                body_preview or "<empty>",
                extra={
                    "provider": provider_type,
                    "model": model_obj.id,
                    "status_code": response.status_code,
                    "path": path,
                    "body_preview": body_preview,
                },
            )
            await response.aclose()
            await client.aclose()
            raise UpstreamError(
                f"EHBP upstream {provider_type} returned {response.status_code} "
                f"for model {model_obj.id}: {body_preview[:200] or '<empty>'}",
                status_code=response.status_code,
            )

        # Check for usage metrics in the response header (non-streaming case).
        # For streaming requests, usage is delivered as an HTTP trailer after
        # the body completes and is not available here — fall back to max-cost.
        usage_header = response.headers.get(_RESPONSE_USAGE_HEADER)
        usage_dict = parse_tinfoil_usage_metrics(usage_header)

        if usage_dict is not None:
            logger.info(
                "EHBP usage metrics received, finalizing with actual token cost",
                extra={
                    "model": model_obj.id,
                    "provider": provider_type,
                    "usage": usage_dict,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )
            await adjust_payment_for_tokens(
                key,
                {"model": model_obj.id, "usage": usage_dict},
                session,
                max_cost_for_model,
            )
        else:
            await finalize_ehbp_max_cost_payment(
                key, session, max_cost_for_model, model_obj.id
            )

        background_tasks = BackgroundTasks()
        background_tasks.add_task(response.aclose)
        background_tasks.add_task(client.aclose)

        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=dict(response.headers),
            background=background_tasks,
        )
    except UpstreamError:
        await client.aclose()
        raise
    except httpx.RequestError as exc:
        await client.aclose()
        raise UpstreamError(
            f"Error connecting to EHBP upstream: {type(exc).__name__}",
            status_code=502,
        ) from exc
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(
            "Unexpected error in EHBP upstream forwarding",
            extra={
                "error": str(exc),
                "error_type": type(exc).__name__,
                "method": request.method,
                "url": target.url,
                "path": path,
                "traceback": tb,
            },
        )
        await client.aclose()
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
    header (non-streaming), the refund is computed from the actual token cost
    instead of max_cost_for_model. For streaming requests, usage is only
    available as an HTTP trailer after the body completes and the refund
    falls back to max_cost_for_model.
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
        target_url = _resolve_ehbp_target_url(target.url, path, headers)
        upstream_headers = _strip_proxy_headers({**headers, **dict(target.headers)})
        request_body = await request.body()

        client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=1),
            timeout=None,
        )

        try:
            response = await client.send(
                client.build_request(
                    request.method,
                    target_url,
                    headers=upstream_headers,
                    content=request_body,
                    params=upstream.prepare_params(path, request.query_params),  # type: ignore[attr-defined]
                ),
                stream=True,
            )

            if response.status_code != 200:
                await response.aclose()
                await client.aclose()
                refund_token = await send_cashu_refund(amount, unit, mint, request_id)
                error_response = Response(
                    content=json.dumps(
                        {
                            "error": {
                                "message": "Error forwarding EHBP request to upstream",
                                "type": "upstream_error",
                                "code": response.status_code,
                                "refund_token": refund_token,
                            }
                        }
                    ),
                    status_code=response.status_code,
                    media_type="application/json",
                )
                error_response.headers["X-Cashu"] = refund_token
                return error_response

            # Compute refund from actual usage when available (non-streaming),
            # otherwise fall back to max_cost_for_model.
            usage_header = response.headers.get(_RESPONSE_USAGE_HEADER)
            actual_cost_msats = await _compute_ehbp_actual_cost(
                usage_header, model_obj, max_cost_for_model
            )
            refund_amount = amount - _msats_to_unit_amount(actual_cost_msats, unit)
            response_headers = _strip_proxy_headers(dict(response.headers))
            if refund_amount > 0:
                response_headers["X-Cashu"] = await send_cashu_refund(
                    refund_amount, unit, mint, request_id
                )

            background_tasks = BackgroundTasks()
            background_tasks.add_task(response.aclose)
            background_tasks.add_task(client.aclose)

            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                headers=response_headers,
                background=background_tasks,
            )
        except Exception:
            await client.aclose()
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
