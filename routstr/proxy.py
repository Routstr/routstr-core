import json
import os
import re
import traceback
from typing import AsyncGenerator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .auth import (
    adjust_payment_for_tokens,
    get_bearer_token_key,
    pay_for_request,
    revert_pay_for_request,
    validate_bearer_key,
)
from .core import get_logger
from .core.db import ApiKey, AsyncSession, create_session, get_session
from .core.settings import settings
from .payment.helpers import (
    check_token_balance,
    create_error_response,
    get_max_cost_for_model,
    prepare_upstream_params,
)
from .payment.x_cashu import XCashuUpstreamProvider
from .request import RoutstrRequest
from .upstream import UpstreamProvider

logger = get_logger(__name__)
proxy_router = APIRouter()


def find_upstream_provider(
    model_id: str, cost_filters: dict | None = None
) -> UpstreamProvider:
    providers = [
        XCashuUpstreamProvider(
            url=settings.upstream_base_url,
            api_key=settings.upstream_api_key,
        ),
    ]
    return providers[0]


async def handle_streaming_chat_completion(
    response: httpx.Response, key: ApiKey, max_cost_for_model: int
) -> StreamingResponse:
    """Handle streaming chat completion responses with token-based pricing."""
    logger.info(
        "Processing streaming chat completion",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "key_balance": key.balance,
            "response_status": response.status_code,
        },
    )

    async def stream_with_cost(max_cost_for_model: int) -> AsyncGenerator[bytes, None]:
        stored_chunks: list[bytes] = []
        usage_finalized: bool = False
        last_model_seen: str | None = None

        async def finalize_without_usage() -> bytes | None:
            nonlocal usage_finalized
            if usage_finalized:
                return None
            async with create_session() as new_session:
                fresh_key = await new_session.get(key.__class__, key.hashed_key)
                if not fresh_key:
                    return None
                try:
                    fallback: dict = {
                        "model": last_model_seen or "unknown",
                        "usage": None,
                    }
                    cost_data = await adjust_payment_for_tokens(
                        fresh_key, fallback, new_session, max_cost_for_model
                    )
                    usage_finalized = True
                    logger.info(
                        "Finalized streaming payment without explicit usage",
                        extra={
                            "key_hash": key.hashed_key[:8] + "...",
                            "cost_data": cost_data,
                            "balance_after_adjustment": fresh_key.balance,
                        },
                    )
                    return f"data: {json.dumps({'cost': cost_data})}\n\n".encode()
                except Exception as cost_error:
                    logger.error(
                        "Error finalizing payment without usage",
                        extra={
                            "error": str(cost_error),
                            "error_type": type(cost_error).__name__,
                            "key_hash": key.hashed_key[:8] + "...",
                        },
                    )
                    return None

        try:
            async for chunk in response.aiter_bytes():
                stored_chunks.append(chunk)
                # Opportunistically capture model id
                try:
                    for part in re.split(b"data: ", chunk):
                        if not part or part.strip() in (b"[DONE]", b""):
                            continue
                        try:
                            obj = json.loads(part)
                            if isinstance(obj, dict) and obj.get("model"):
                                last_model_seen = str(obj.get("model"))
                        except json.JSONDecodeError:
                            pass
                except Exception:
                    pass

                yield chunk

            logger.debug(
                "Streaming completed, analyzing usage data",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "chunks_count": len(stored_chunks),
                },
            )

            # Process stored chunks to find usage data from the tail
            for i in range(len(stored_chunks) - 1, -1, -1):
                chunk = stored_chunks[i]
                if not chunk:
                    continue
                try:
                    events = re.split(b"data: ", chunk)
                    for event_data in events:
                        if not event_data or event_data.strip() in (b"[DONE]", b""):
                            continue
                        try:
                            data = json.loads(event_data)
                            if isinstance(data, dict) and data.get("model"):
                                last_model_seen = str(data.get("model"))
                            if isinstance(data, dict) and isinstance(
                                data.get("usage"), dict
                            ):
                                async with create_session() as new_session:
                                    fresh_key = await new_session.get(
                                        key.__class__, key.hashed_key
                                    )
                                    if fresh_key:
                                        try:
                                            cost_data = await adjust_payment_for_tokens(
                                                fresh_key,
                                                data,
                                                new_session,
                                                max_cost_for_model,
                                            )
                                            usage_finalized = True
                                            logger.info(
                                                "Token adjustment completed for streaming",
                                                extra={
                                                    "key_hash": key.hashed_key[:8]
                                                    + "...",
                                                    "cost_data": cost_data,
                                                    "balance_after_adjustment": fresh_key.balance,
                                                },
                                            )
                                            yield f"data: {json.dumps({'cost': cost_data})}\n\n".encode()
                                        except Exception as cost_error:
                                            logger.error(
                                                "Error adjusting payment for streaming tokens",
                                                extra={
                                                    "error": str(cost_error),
                                                    "error_type": type(
                                                        cost_error
                                                    ).__name__,
                                                    "key_hash": key.hashed_key[:8]
                                                    + "...",
                                                },
                                            )
                                break
                        except json.JSONDecodeError:
                            continue
                except Exception as e:
                    logger.error(
                        "Error processing streaming response chunk",
                        extra={
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "key_hash": key.hashed_key[:8] + "...",
                        },
                    )

            # If we reach here without finding usage, finalize with max-cost
            if not usage_finalized:
                maybe_cost_event = await finalize_without_usage()
                if maybe_cost_event is not None:
                    yield maybe_cost_event

        except Exception as stream_error:
            # On stream interruption, still finalize reservation with max-cost
            logger.warning(
                "Streaming interrupted; finalizing without usage",
                extra={
                    "error": str(stream_error),
                    "error_type": type(stream_error).__name__,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )
            await finalize_without_usage()
            raise

    return StreamingResponse(
        stream_with_cost(max_cost_for_model),
        status_code=response.status_code,
        headers=dict(response.headers),
    )


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy_to_upstream(
    request: Request, path: str, db_session: AsyncSession = Depends(get_session)
) -> Response | StreamingResponse:
    request_body = json.loads(await request.body())
    headers = dict(request.headers)
    routstr_request = RoutstrRequest.from_request(request_body, headers, path, request)

    # TODO: should raise proper HTTPExceptions instead of ValueError
    # TODO: add better validation and error handling
    routstr_request.validate()

    required_reserved_balance_msats = get_max_cost_for_model(
        model=routstr_request.model_id
    )

    # TODO fix multiple db_session commits

    # without claiming the token if new token provided, else just check db balance
    await raise_not_enough_balance(
        routstr_request=routstr_request,
        required_balance_msats=required_reserved_balance_msats,
        db_session=db_session,
    )

    # if new cashu token is provided, it is redeemed here
    # temp_balance = await routstr_request.get_or_create_balance(db_session)

    upstream_provider = find_upstream_provider(routstr_request.model_id)

    await routstr_request.reserve_balance(db_session, required_reserved_balance_msats)

    response = await upstream_provider.forward(routstr_request, db_session)

    if response.status_code != 200:
        await revert_pay_for_request(
            await routstr_request.get_or_create_balance(db_session),
            db_session,
            required_reserved_balance_msats,
        )
        raise ValueError("Upstream request failed")  # TODO change to HTTPException

    return response


async def raise_not_enough_balance(
    routstr_request: RoutstrRequest,
    required_balance_msats: int,
    db_session: AsyncSession,
) -> None:
    if routstr_request.cashu_token:
        msat_multiplier = 1000 if routstr_request.cashu_token.unit == "sat" else 1
        token_amount_msats = routstr_request.cashu_token.amount * msat_multiplier
        if required_balance_msats > token_amount_msats:
            raise HTTPException(
                status_code=413,
                detail={
                    "reason": "Insufficient balance",
                    "amount_required_msat": required_balance_msats,
                    "model_id": routstr_request.model_id,
                    "type": "minimum_balance_required",
                },
            )
    elif routstr_request.balance_id:
        balance = await routstr_request.get_or_create_balance(db_session)
        if required_balance_msats > balance.balance:
            raise HTTPException(
                status_code=413,
                detail={
                    "reason": "Insufficient balance",
                    "amount_required_msat": required_balance_msats,
                    "model_id": routstr_request.model_id,
                    "type": "minimum_balance_required",
                },
            )
    else:
        raise HTTPException(
            status_code=400,
            detail={"reason": "No balance found", "type": "no_balance_found"},
        )


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(
    request: Request, path: str, session: AsyncSession = Depends(get_session)
) -> Response | StreamingResponse:
    """Main proxy endpoint handler."""
    request_body = await request.body()
    headers = dict(request.headers)
    upstream_provider = XCashuUpstreamProvider(
        url=os.getenv("UPSTREAM_BASE_URL") or "",
        api_key=os.getenv("UPSTREAM_API_KEY") or "",
    )

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
    max_cost_for_model = get_max_cost_for_model(model=model)
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
        return await upstream_provider.x_cashu_handler(
            request, x_cashu, path, max_cost_for_model
        )

    elif auth := headers.get("authorization", None):
        logger.debug(
            "Processing bearer token authentication",
            extra={
                "path": path,
                "token_preview": auth[:20] + "..." if len(auth) > 20 else auth,
            },
        )
        key = await get_bearer_token_key(headers, path, session, auth)

    # TODO: why is this needed? can we remove it?
    # else:
    #     if request.method not in ["GET"]:
    #         logger.warning(
    #             "Unauthorized request - no authentication provided",
    #             extra={"method": request.method, "path": path},
    #         )
    #         return Response(
    #             content=json.dumps({"detail": "Unauthorized"}),
    #             status_code=401,
    #             media_type="application/json",
    #         )

    #     logger.debug("Processing unauthenticated GET request", extra={"path": path})
    #     headers = upstream_provider.prepare_upstream_headers(dict(request.headers))
    #     return await upstream_provider.forward_to_upstream(
    #         request,
    #         path,
    #         headers,
    #         request_body,
    #         key,
    #         max_cost_for_model,
    #         session,
    #     )

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
    headers = upstream_provider.prepare_upstream_headers(dict(request.headers))

    # Forward to upstream and handle response
    response = await upstream_provider.forward_to_upstream(
        request, path, headers, request_body, key, max_cost_for_model, session
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
                "upstream_response": response.body
                if hasattr(response, "body")
                else None,
            },
        )
        request_id = (
            request.state.request_id if hasattr(request.state, "request_id") else None
        )
        raise HTTPException(
            status_code=502,
            detail=f"Upstream request failed, please contact support with request id: {request_id}",
        )

    return response


async def get_bearer_token_key_local(
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


async def forward_get_to_upstream(
    request: Request,
    path: str,
    headers: dict,
) -> Response | StreamingResponse:
    """Forward request to upstream and handle the response."""
    if path.startswith("v1/"):
        path = path.replace("v1/", "")

    url = f"{settings.upstream_base_url}/{path}"

    logger.info(
        "Forwarding GET request to upstream",
        extra={"url": url, "method": request.method, "path": path},
    )

    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=1),
        timeout=None,
    ) as client:
        try:
            response = await client.send(
                client.build_request(
                    request.method,
                    url,
                    headers=headers,
                    content=request.stream(),
                    params=prepare_upstream_params(path, request.query_params),
                ),
            )

            logger.info(
                "GET request forwarded successfully",
                extra={"path": path, "status_code": response.status_code},
            )

            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                headers=dict(response.headers),
            )
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error(
                "Error forwarding GET request",
                extra={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "method": request.method,
                    "url": url,
                    "path": path,
                    "query_params": dict(request.query_params),
                    "traceback": tb,
                },
            )
            return create_error_response(
                "internal_error",
                "An unexpected server error occurred",
                500,
                request=request,
            )
