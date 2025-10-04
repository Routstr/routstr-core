import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .auth import pay_for_request, revert_pay_for_request, validate_bearer_key
from .core import get_logger
from .core.db import ApiKey, AsyncSession, get_session
from .core.settings import settings
from .payment.helpers import (
    calculate_discounted_max_cost,
    check_token_balance,
    create_error_response,
    get_max_cost_for_model,
)
from .upstream import (
    UpstreamProvider,
    handle_non_streaming_chat_completion,
    handle_streaming_chat_completion,
    map_upstream_error_response,
)

logger = get_logger(__name__)
proxy_router = APIRouter()

upstream = UpstreamProvider(
    base_url=settings.upstream_base_url,
    api_key=settings.upstream_api_key,
    chat_completions_api_version=settings.chat_completions_api_version,
)


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(
    request: Request, path: str, session: AsyncSession = Depends(get_session)
) -> Response | StreamingResponse:
    """Main proxy endpoint handler."""
    request_body = await request.body()
    headers = dict(request.headers)

    if "x-cashu" not in headers and "authorization" not in headers.keys():
        return create_error_response(
            "unauthorized", "Unauthorized", 401, request=request
        )

    logger.info(
        "Received proxy request",
        extra={
            "method": request.method,
            "path": path,
            "client_host": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", "unknown")[:100],
        },
    )

    # Parse JSON body if present, handle empty/invalid JSON
    request_body_dict = {}
    if request_body:
        try:
            request_body_dict = json.loads(request_body)
            logger.debug(
                "Request body parsed",
                extra={
                    "path": path,
                    "body_keys": list(request_body_dict.keys()),
                    "model": request_body_dict.get("model", "not_specified"),
                },
            )
        except json.JSONDecodeError as e:
            logger.error(
                "Invalid JSON in request body",
                extra={
                    "error": str(e),
                    "path": path,
                    "body_preview": request_body[:200].decode(errors="ignore")
                    if request_body
                    else "empty",
                },
            )
            return Response(
                content=json.dumps(
                    {"error": {"type": "invalid_request_error", "code": "invalid_json"}}
                ),
                status_code=400,
                media_type="application/json",
            )

    model = request_body_dict.get("model", "unknown")
    _max_cost_for_model = await get_max_cost_for_model(model=model, session=session)
    max_cost_for_model = await calculate_discounted_max_cost(
        _max_cost_for_model, request_body_dict, session
    )
    check_token_balance(headers, request_body_dict, max_cost_for_model)

    # Handle authentication
    if x_cashu := headers.get("x-cashu", None):
        logger.info(
            "Processing X-Cashu payment",
            extra={
                "path": path,
                "token_preview": x_cashu[:20] + "..." if len(x_cashu) > 20 else x_cashu,
            },
        )
        return await upstream.handle_x_cashu(request, x_cashu, path, max_cost_for_model)

    elif auth := headers.get("authorization", None):
        logger.debug(
            "Processing bearer token authentication",
            extra={
                "path": path,
                "token_preview": auth[:20] + "..." if len(auth) > 20 else auth,
            },
        )
        key = await get_bearer_token_key(headers, path, session, auth)

    else:
        if request.method not in ["GET"]:
            logger.warning(
                "Unauthorized request - no authentication provided",
                extra={"method": request.method, "path": path},
            )
            return Response(
                content=json.dumps({"detail": "Unauthorized"}),
                status_code=401,
                media_type="application/json",
            )

        logger.debug("Processing unauthenticated GET request", extra={"path": path})
        # TODO: why is this needed? can we remove it?
        headers = upstream.prepare_headers(dict(request.headers))
        return await upstream.forward_get_request(
            request, path, headers, map_upstream_error_response
        )

    # Only pay for request if we have request body data (for completions endpoints)
    if request_body_dict:
        logger.info(
            "Processing payment for request",
            extra={
                "path": path,
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance_before": key.balance,
                "model": request_body_dict.get("model", "unknown"),
            },
        )

        try:
            await pay_for_request(key, max_cost_for_model, session)
            logger.info(
                "Payment processed successfully",
                extra={
                    "path": path,
                    "key_hash": key.hashed_key[:8] + "...",
                    "key_balance_after": key.balance,
                    "model": request_body_dict.get("model", "unknown"),
                },
            )
        except Exception as e:
            logger.error(
                "Payment processing failed",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "path": path,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )
            raise

    # Prepare headers for upstream
    headers = upstream.prepare_headers(dict(request.headers))

    # Forward to upstream and handle response
    response = await upstream.forward_request(
        request,
        path,
        headers,
        request_body,
        key,
        max_cost_for_model,
        session,
        map_upstream_error_response,
        handle_streaming_chat_completion,
        handle_non_streaming_chat_completion,
    )

    if response.status_code != 200:
        await revert_pay_for_request(key, session, max_cost_for_model)
        logger.warning(
            "Upstream request failed, revert payment",
            extra={
                "status_code": response.status_code,
                "path": path,
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance": key.balance,
                "max_cost_for_model": max_cost_for_model,
                "upstream_headers": response.headers
                if hasattr(response, "headers")
                else None,
            },
        )
        # Return the mapped error response generated earlier rather than masking with 502
        return response

    return response


async def get_bearer_token_key(
    headers: dict, path: str, session: AsyncSession, auth: str
) -> ApiKey:
    """Handle bearer token authentication proxy requests."""
    bearer_key = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    refund_address = headers.get("Refund-LNURL", None)
    key_expiry_time = headers.get("Key-Expiry-Time", None)

    logger.debug(
        "Processing bearer token",
        extra={
            "path": path,
            "has_refund_address": bool(refund_address),
            "has_expiry_time": bool(key_expiry_time),
            "bearer_key_preview": bearer_key[:20] + "..."
            if len(bearer_key) > 20
            else bearer_key,
        },
    )

    # Validate key_expiry_time header
    if key_expiry_time:
        try:
            key_expiry_time = int(key_expiry_time)  # type: ignore
            logger.debug(
                "Key expiry time validated",
                extra={"expiry_time": key_expiry_time, "path": path},
            )
        except ValueError:
            logger.error(
                "Invalid Key-Expiry-Time header",
                extra={"key_expiry_time": key_expiry_time, "path": path},
            )
            raise HTTPException(
                status_code=400,
                detail="Invalid Key-Expiry-Time: must be a valid Unix timestamp",
            )
        if not refund_address:
            logger.error(
                "Missing Refund-LNURL header with Key-Expiry-Time",
                extra={"path": path, "expiry_time": key_expiry_time},
            )
            raise HTTPException(
                status_code=400,
                detail="Error: Refund-LNURL header required when using Key-Expiry-Time",
            )
    else:
        key_expiry_time = None

    try:
        key = await validate_bearer_key(
            bearer_key,
            session,
            refund_address,
            key_expiry_time,  # type: ignore
        )
        logger.info(
            "Bearer token validated successfully",
            extra={
                "path": path,
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance": key.balance,
            },
        )
        return key
    except Exception as e:
        logger.error(
            "Bearer token validation failed",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "path": path,
                "bearer_key_preview": bearer_key[:20] + "..."
                if len(bearer_key) > 20
                else bearer_key,
            },
        )
        raise
