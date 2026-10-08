"""Dedicated buffered Images API flow; never enters chat retry/estimation paths."""

import asyncio
import json
import math
import time
from typing import Any, cast

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response

from ..auth import (
    adjust_payment_for_tokens,
    pay_for_request,
    revert_pay_for_request,
)
from ..core import get_logger
from ..core.db import AsyncSession
from ..core.settings import settings
from ..payment.helpers import create_error_response
from ..payment.images import (
    ImageQuote,
    ImageRequestError,
    calculate_image_cost,
    quote_image_request,
)
from ..payment.price import sats_usd_price
from .base import (
    _inject_cost_into_usage,
    _inject_cost_response_headers,
    _published_cost,
)
from .model_paths import is_openrouter_base_url

logger = get_logger(__name__)


def _error(request: Request, code: str, message: str, status: int = 400) -> Response:
    return create_error_response(
        "invalid_request" if status < 500 else "upstream_error",
        message,
        status,
        request=request,
        code=code,
    )


async def generate_buffered_image(*, upstream: Any, quote: ImageQuote) -> dict:
    """Exactly one POST, bounded decompressed bytes and total lifetime, no retries."""
    url = upstream.base_url.rstrip("/") + "/images"
    headers = upstream.prepare_headers(
        {"content-type": "application/json", "accept": "application/json"}
    )
    # The transport has no automatic retries; redirects must not move credentials.
    async with asyncio.timeout(settings.image_generation_timeout_seconds):
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=settings.image_generation_timeout_seconds
        ) as client:
            async with client.stream(
                "POST", url, headers=headers, content=quote.body_json
            ) as response:
                if response.status_code != 200:
                    raise ImageRequestError(
                        "image_upstream_error", "Image generation failed upstream", 502
                    )
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type != "application/json" and not (
                    content_type.startswith("application/")
                    and content_type.endswith("+json")
                ):
                    raise ImageRequestError(
                        "image_invalid_response",
                        "Expected a buffered JSON image response",
                        502,
                    )
                declared = response.headers.get("content-length", "")
                if (
                    declared.isdigit()
                    and int(declared) > settings.image_max_response_bytes
                ):
                    raise ImageRequestError(
                        "image_response_too_large",
                        "Image response exceeds the configured limit",
                        502,
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > settings.image_max_response_bytes:
                        raise ImageRequestError(
                            "image_response_too_large",
                            "Image response exceeds the configured limit",
                            502,
                        )
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ImageRequestError(
            "image_invalid_response", "Invalid image JSON response", 502
        ) from exc
    if not isinstance(payload, dict):
        raise ImageRequestError(
            "image_invalid_response", "Expected an image response object", 502
        )
    return payload


def _publish(
    payload: dict, cost: Any, upstream: Any, *, balance: int | None = None
) -> Response:
    upstream._apply_provider_field(payload)
    _inject_cost_into_usage(payload, cost)
    published = _published_cost(cost)
    published["sats_cost"] = published["total_msats"] // 1000
    if balance is not None:
        published["remaining_balance_msats"] = balance
        payload["usage"]["remaining_balance_msats"] = balance
    payload["cost"] = published
    # Replace rather than trust upstream metadata shape.
    payload["metadata"] = {"routstr": {"cost": published.copy()}}
    headers: dict[str, str] = {}
    _inject_cost_response_headers(headers, cost)
    return Response(json.dumps(payload), headers=headers, media_type="application/json")


async def forward_image_request(
    request: Request, session: AsyncSession, request_body: bytes
) -> Response:
    """Validate/quote before moving money, then dispatch without provider failover."""
    from ..proxy import get_bearer_token_key, get_candidates

    if "x-cashu" in request.headers:
        return _error(
            request,
            "x_cashu_unsupported_endpoint",
            "Images currently require bearer authentication; the Cashu token was not redeemed",
        )
    if (
        not settings.image_generation_enabled
        or not math.isfinite(settings.image_max_request_usd)
        or settings.image_max_request_usd <= 0
    ):
        return _error(
            request, "image_generation_disabled", "Image generation is not enabled", 503
        )
    if (
        request.query_params
        or "ehbp-encapsulated-key" in request.headers
        or "x-routstr-model-path" in request.headers
    ):
        return _error(
            request,
            "image_unsupported_request",
            "Images require plain JSON without query parameters or model-path overrides",
        )
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        return _error(
            request, "image_unsupported_request", "Images require application/json"
        )
    try:
        body = json.loads(request_body)
    except (ValueError, UnicodeError):
        return _error(request, "image_invalid_request", "Invalid JSON body")
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("model"), str)
        or not body["model"].strip()
    ):
        return _error(request, "image_invalid_request", "A model string is required")
    authorization = request.headers.get("authorization", "")
    auth_parts = authorization.split()
    if len(auth_parts) != 2 or auth_parts[0].lower() != "bearer" or not auth_parts[1]:
        raise HTTPException(401, "Unauthorized")
    model_id = body["model"]
    request.state.model = model_id
    candidates = get_candidates(model_id) or []
    quote_error: ImageRequestError | None = None
    chosen = None
    now = time.time()
    try:
        usd_per_sat = sats_usd_price()
        for model, upstream in candidates:
            if (
                not model.enabled
                or upstream.provider_type != "openrouter"
                or not is_openrouter_base_url(upstream.base_url)
            ):
                continue
            capability = model.api_capabilities.get("images")
            if capability is None:
                continue
            caps = (
                capability.dict()
                if hasattr(capability, "dict")
                else cast(dict[str, Any], capability)
            )
            fetched_at = caps.get("fetched_at", 0)
            if (
                not isinstance(fetched_at, (int, float))
                or fetched_at > now + 60
                or now - fetched_at > settings.image_capabilities_max_age_seconds
            ):
                continue
            try:
                quote = quote_image_request(
                    body,
                    upstream_model_id=upstream.transform_model_name(model.id),
                    capabilities=caps,
                    provider_fee=upstream.provider_fee,
                    usd_per_sat=usd_per_sat,
                    max_request_usd=settings.image_max_request_usd,
                )
            except ImageRequestError as exc:
                quote_error = exc
                continue
            chosen = model, upstream, quote
            break
        if chosen is None:
            if quote_error:
                raise quote_error
            return _error(
                request,
                "image_capabilities_unavailable",
                "No enabled OpenRouter image endpoint has fresh capabilities",
            )
        model, upstream, quote = chosen
        request.state.provider = upstream.provider_type
        request.state.model = model.id
        key = await get_bearer_token_key(
            dict(request.headers),
            "images",
            session,
            authorization,
            min_cost=quote.reserved_msats,
            model_id=model_id,
        )
        snapshot = await pay_for_request(key, quote.reserved_msats, session)
        settled = False
        try:
            await session.commit()
            payload = await generate_buffered_image(upstream=upstream, quote=quote)
            payload["model"] = quote.upstream_model_id
            cost = calculate_image_cost(payload, quote=quote)
            result = await adjust_payment_for_tokens(
                key,
                payload,
                session,
                snapshot.reserved_msats,
                model,
                upstream.provider_fee,
                snapshot,
                precomputed_cost=cost,
            )
            settled = True
            await session.refresh(key)
            return _publish(payload, result, upstream, balance=key.balance)
        finally:
            if not settled:
                await asyncio.shield(
                    revert_pay_for_request(
                        key, session, snapshot.reserved_msats, snapshot
                    )
                )
    except ImageRequestError as exc:
        return _error(request, exc.code, exc.message, exc.status_code)
    except (httpx.HTTPError, TimeoutError):
        return _error(
            request,
            "image_upstream_error",
            "Image generation failed or timed out; request was not retried",
            502,
        )
