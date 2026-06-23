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
from .tinfoil_trailer import forward_with_trailer

logger = get_logger(__name__)

# Headers that the Tinfoil SDK sends to tell the proxy where to forward the
# encrypted request, and the request/response usage-metrics pair.
_ENCLAVE_URL_HEADER = "X-Tinfoil-Enclave-Url"
_REQUEST_USAGE_HEADER = "X-Tinfoil-Request-Usage-Metrics"
_RESPONSE_USAGE_HEADER = "X-Tinfoil-Usage-Metrics"
_TINFOIL_PROVIDER_TYPE = "tinfoil"
_TINFOIL_ALLOWED_ENCLAVE_HOST_SUFFIX = ".tinfoil.sh"
_TINFOIL_ALLOWED_ENCLAVE_HOSTS = {"tinfoil.sh"}

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
) -> str:
    """Override Tinfoil forwarding URL with a validated enclave URL.

    When the Tinfoil SDK is configured with a proxy ``baseURL``, it sends the
    actual enclave URL in ``X-Tinfoil-Enclave-Url``. The override is honored
    only for the Tinfoil provider and only when the URL is an HTTPS Tinfoil
    hostname, so the header cannot redirect other EHBP providers or leak
    upstream API keys to arbitrary origins.
    """
    enclave_url = _get_header_case_insensitive(headers, _ENCLAVE_URL_HEADER)
    if not enclave_url:
        return target_url

    if provider_type != _TINFOIL_PROVIDER_TYPE:
        logger.warning(
            "Ignoring X-Tinfoil-Enclave-Url for non-Tinfoil EHBP provider",
            extra={"provider": provider_type or "unknown"},
        )
        return target_url

    validated_base_url = _validated_tinfoil_enclave_base_url(enclave_url)
    if validated_base_url is None:
        logger.warning(
            "Rejected invalid X-Tinfoil-Enclave-Url",
            extra={"provider": provider_type or "unknown"},
        )
        raise UpstreamError(
            "Invalid X-Tinfoil-Enclave-Url: expected an HTTPS tinfoil.sh URL",
            status_code=400,
        )

    return f"{validated_base_url}/{path.lstrip('/')}"


def _strip_proxy_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove proxy-routing headers that must not reach the upstream enclave."""
    clean = {}
    for key, value in headers.items():
        if key.lower() not in _PROXY_ONLY_HEADERS:
            clean[key] = value
    return clean


def _prepare_ehbp_upstream_headers(
    headers: dict[str, str], target_headers: Mapping[str, str]
) -> dict[str, str]:
    """Merge safe request headers with provider-controlled EHBP target headers.

    Client-supplied proxy control headers must be stripped, but provider-added
    target headers such as ``X-Tinfoil-Request-Usage-Metrics: true`` must still
    reach the upstream enclave. Strip first, then merge target headers so
    callers cannot spoof proxy controls while providers can opt into protocol
    features.
    """
    return {**_strip_proxy_headers(headers), **dict(target_headers)}


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
        logger.warning(
            "EHBP calculate_cost returned MaxCostData (no model pricing), "
            "falling back to max cost",
            extra={
                "model": model_obj.id,
                "max_cost_for_model": max_cost_for_model,
                "usage": usage_dict,
                "cost_total_msats": cost.total_msats,
            },
        )
        return max_cost_for_model
    if isinstance(cost, CostData):
        actual = max(int(cost.total_msats), int(settings.min_request_msat))
        clamped = min(actual, max_cost_for_model)
        logger.info(
            "EHBP actual cost computed from usage metrics",
            extra={
                "model": model_obj.id,
                "usage": usage_dict,
                "cost_total_msats": cost.total_msats,
                "clamped_msats": clamped,
                "max_cost_for_model": max_cost_for_model,
            },
        )
        return clamped
    # CostDataError
    logger.warning(
        "EHBP usage cost calculation error, falling back to max cost",
        extra={
            "model": model_obj.id,
            "error": getattr(cost, "message", str(cost)),
        },
    )
    return max_cost_for_model


def _extract_usage_from_response(
    resp_headers: list[tuple[str, str]],
    trailers: list[tuple[str, str]],
) -> str | None:
    """Find X-Tinfoil-Usage-Metrics in response headers or trailers.

    Non-streaming responses put usage in the response header. Streaming
    responses put it in an HTTP trailer (declared via the ``Trailer:`` header).
    httpx/httpcore silently discard trailers, so we use h11 directly when
    forwarding EHBP requests.
    """
    # Check response headers first (non-streaming case)
    for k, v in resp_headers:
        if k.lower() == _RESPONSE_USAGE_HEADER.lower():
            return v
    # Check trailers (streaming case)
    for k, v in trailers:
        if k.lower() == _RESPONSE_USAGE_HEADER.lower():
            return v
    return None


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
    trailer (streaming). Usage is captured from both response headers and HTTP
    trailers via an h11-based client (httpx silently discards trailers).
    """
    target = upstream.get_ehbp_forwarding_target(path, model_obj)  # type: ignore[attr-defined]

    provider_type = getattr(upstream, "provider_type", "unknown")
    target_url = _resolve_ehbp_target_url(target.url, path, headers, provider_type)
    upstream_headers = _prepare_ehbp_upstream_headers(headers, target.headers)

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
        usage_header = _extract_usage_from_response(resp.headers, resp.trailers)
        usage_dict = parse_tinfoil_usage_metrics(usage_header)
        usage_source = (
            "header"
            if any(k.lower() == _RESPONSE_USAGE_HEADER.lower() for k, _ in resp.headers)
            else ("trailer" if resp.trailers else "none")
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
            await adjust_payment_for_tokens(
                key,
                {"model": model_obj.id, "usage": usage_dict},
                session,
                max_cost_for_model,
            )
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
                key, session, max_cost_for_model, model_obj.id
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
        target_url = _resolve_ehbp_target_url(target.url, path, headers, provider_type)
        upstream_headers = _prepare_ehbp_upstream_headers(headers, target.headers)
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
            usage_header = _extract_usage_from_response(resp.headers, resp.trailers)
            usage_source = (
                "header"
                if any(
                    k.lower() == _RESPONSE_USAGE_HEADER.lower() for k, _ in resp.headers
                )
                else ("trailer" if resp.trailers else "none")
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

            actual_cost_msats = await _compute_ehbp_actual_cost(
                usage_header, model_obj, max_cost_for_model
            )
            refund_amount = amount - _msats_to_unit_amount(actual_cost_msats, unit)
            logger.info(
                "EHBP X-Cashu refund computed",
                extra={
                    "model": model_obj.id,
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
