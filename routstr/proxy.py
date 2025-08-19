import json
import re
import traceback
from typing import AsyncGenerator

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .auth import (
    adjust_payment_for_tokens,
    pay_for_request,
    revert_pay_for_request,
    validate_bearer_key,
)
from .core import get_logger
from .core.db import ApiKey, AsyncSession, create_session, get_session
from .payment.helpers import (
    UPSTREAM_BASE_URL,
    check_token_balance,
    create_error_response,
    get_cost_per_request,
    get_upstream_url_for_request,
    prepare_upstream_headers,
)
from .payment.x_cashu import x_cashu_handler

logger = get_logger(__name__)
proxy_router = APIRouter()


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
        # Store all chunks to analyze
        stored_chunks = []

        async for chunk in response.aiter_bytes():
            # Store chunk for later analysis
            stored_chunks.append(chunk)

            # Pass through each chunk to client
            yield chunk

        logger.debug(
            "Streaming completed, analyzing usage data",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "chunks_count": len(stored_chunks),
            },
        )

        # Process stored chunks to find usage data
        # Start from the end and work backwards
        for i in range(len(stored_chunks) - 1, -1, -1):
            chunk = stored_chunks[i]
            if not chunk or chunk == b"":
                continue

            try:
                # Split by "data: " to get individual SSE events
                events = re.split(b"data: ", chunk)
                for event_data in events:
                    if (
                        not event_data
                        or event_data.strip() == b"[DONE]"
                        or event_data.strip() == b""
                    ):
                        continue

                    try:
                        data = json.loads(event_data)
                        if (
                            "usage" in data
                            and data["usage"] is not None
                            and isinstance(data["usage"], dict)
                        ):
                            logger.info(
                                "Found usage data in streaming response",
                                extra={
                                    "key_hash": key.hashed_key[:8] + "...",
                                    "usage_data": data["usage"],
                                    "model": data.get("model", "unknown"),
                                },
                            )

                            # Found usage data, calculate cost
                            # Create a new session for this operation
                            async with create_session() as new_session:
                                # Re-fetch the key in the new session
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
                                        logger.info(
                                            "Token adjustment completed for streaming",
                                            extra={
                                                "key_hash": key.hashed_key[:8] + "...",
                                                "cost_data": cost_data,
                                                "balance_after_adjustment": fresh_key.balance,
                                            },
                                        )
                                        # Format as SSE and yield
                                        cost_json = json.dumps({"cost": cost_data})
                                        yield f"data: {cost_json}\n\n".encode()
                                    except Exception as cost_error:
                                        logger.error(
                                            "Error adjusting payment for streaming tokens",
                                            extra={
                                                "error": str(cost_error),
                                                "error_type": type(cost_error).__name__,
                                                "key_hash": key.hashed_key[:8] + "...",
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

    return StreamingResponse(
        stream_with_cost(max_cost_for_model),
        status_code=response.status_code,
        headers=dict(response.headers),
    )


async def handle_non_streaming_chat_completion(
    response: httpx.Response,
    key: ApiKey,
    session: AsyncSession,
    deducted_max_cost: int,
) -> Response:
    """Handle non-streaming chat completion responses with token-based pricing."""
    logger.info(
        "Processing non-streaming chat completion",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "key_balance": key.balance,
            "response_status": response.status_code,
        },
    )

    try:
        content = await response.aread()
        response_json = json.loads(content)

        logger.debug(
            "Parsed response JSON",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "model": response_json.get("model", "unknown"),
                "has_usage": "usage" in response_json,
            },
        )

        cost_data = await adjust_payment_for_tokens(
            key, response_json, session, deducted_max_cost
        )
        response_json["cost"] = cost_data

        logger.info(
            "Token adjustment completed for non-streaming",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "cost_data": cost_data,
                "model": response_json.get("model", "unknown"),
                "balance_after_adjustment": key.balance,
            },
        )

        # Keep only standard headers that are safe to pass through
        allowed_headers = {
            "content-type",
            "cache-control",
            "date",
            "vary",
            "access-control-allow-origin",
            "access-control-allow-methods",
            "access-control-allow-headers",
            "access-control-allow-credentials",
            "access-control-expose-headers",
            "access-control-max-age",
        }

        response_headers = {
            k: v for k, v in response.headers.items() if k.lower() in allowed_headers
        }

        return Response(
            content=json.dumps(response_json).encode(),
            status_code=response.status_code,
            headers=response_headers,
            media_type="application/json",
        )
    except json.JSONDecodeError as e:
        logger.error(
            "Failed to parse JSON from upstream response",
            extra={
                "error": str(e),
                "key_hash": key.hashed_key[:8] + "...",
                "content_preview": content[:200].decode(errors="ignore")
                if content
                else "empty",
            },
        )
        raise
    except Exception as e:
        logger.error(
            "Error processing non-streaming chat completion",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "key_hash": key.hashed_key[:8] + "...",
            },
        )
        raise


async def forward_to_upstream(
    request: Request,
    path: str,
    headers: dict,
    request_body: bytes | None,
    key: ApiKey,
    max_cost_for_model: int,
    session: AsyncSession,
) -> Response | StreamingResponse:
    """Forward request to upstream and handle the response."""
    # Parse request body to extract model name for provider URL lookup
    request_body_dict = None
    if request_body:
        try:
            request_body_dict = json.loads(request_body)
        except json.JSONDecodeError:
            pass  # Continue with default URL if body parsing fails

    url = get_upstream_url_for_request(path, request_body_dict)

    logger.info(
        "Forwarding request to upstream",
        extra={
            "url": url,
            "method": request.method,
            "path": path,
            "key_hash": key.hashed_key[:8] + "...",
            "key_balance": key.balance,
            "has_request_body": request_body is not None,
        },
    )

    client = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=1),
        timeout=None,  # No timeout - requests can take as long as needed
    )

    try:
        # Use the pre-read body if available, otherwise stream
        if request_body is not None:
            response = await client.send(
                client.build_request(
                    request.method,
                    url,
                    headers=headers,
                    content=request_body,
                    params=request.query_params,
                ),
                stream=True,
            )
        else:
            response = await client.send(
                client.build_request(
                    request.method,
                    url,
                    headers=headers,
                    content=request.stream(),
                    params=request.query_params,
                ),
                stream=True,
            )

        logger.info(
            "Received upstream response",
            extra={
                "status_code": response.status_code,
                "path": path,
                "key_hash": key.hashed_key[:8] + "...",
                "content_type": response.headers.get("content-type", "unknown"),
            },
        )

        # For chat completions, we need to handle token-based pricing
        if path.endswith("chat/completions"):
            # Check if client requested streaming
            client_wants_streaming = False
            if request_body:
                try:
                    request_data = json.loads(request_body)
                    client_wants_streaming = request_data.get("stream", False)
                    logger.debug(
                        "Chat completion request analysis",
                        extra={
                            "client_wants_streaming": client_wants_streaming,
                            "model": request_data.get("model", "unknown"),
                            "key_hash": key.hashed_key[:8] + "...",
                        },
                    )
                except json.JSONDecodeError:
                    logger.warning(
                        "Failed to parse request body JSON for streaming detection"
                    )

            # Handle both streaming and non-streaming responses
            content_type = response.headers.get("content-type", "")
            upstream_is_streaming = "text/event-stream" in content_type
            is_streaming = client_wants_streaming and upstream_is_streaming

            logger.debug(
                "Response type analysis",
                extra={
                    "is_streaming": is_streaming,
                    "client_wants_streaming": client_wants_streaming,
                    "upstream_is_streaming": upstream_is_streaming,
                    "content_type": content_type,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )

            if is_streaming and response.status_code == 200:
                # Process streaming response and extract cost from the last chunk
                result = await handle_streaming_chat_completion(
                    response, key, max_cost_for_model
                )
                background_tasks = BackgroundTasks()
                background_tasks.add_task(response.aclose)
                background_tasks.add_task(client.aclose)
                result.background = background_tasks
                return result

            elif response.status_code == 200:
                # Handle non-streaming response
                try:
                    return await handle_non_streaming_chat_completion(
                        response, key, session, max_cost_for_model
                    )
                finally:
                    await response.aclose()
                    await client.aclose()

        # For all other responses, stream the response
        background_tasks = BackgroundTasks()
        background_tasks.add_task(response.aclose)
        background_tasks.add_task(client.aclose)

        logger.debug(
            "Streaming non-chat response",
            extra={
                "path": path,
                "status_code": response.status_code,
                "key_hash": key.hashed_key[:8] + "...",
            },
        )

        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=dict(response.headers),
            background=background_tasks,
        )

    except httpx.RequestError as exc:
        await client.aclose()
        error_type = type(exc).__name__
        error_details = str(exc)

        logger.error(
            "HTTP request error to upstream",
            extra={
                "error_type": error_type,
                "error_details": error_details,
                "method": request.method,
                "url": url,
                "path": path,
                "query_params": dict(request.query_params),
                "key_hash": key.hashed_key[:8] + "...",
            },
        )

        # Provide more specific error messages based on the error type
        if isinstance(exc, httpx.ConnectError):
            error_message = "Unable to connect to upstream service"
        elif isinstance(exc, httpx.TimeoutException):
            error_message = "Upstream service request timed out"
        elif isinstance(exc, httpx.NetworkError):
            error_message = "Network error while connecting to upstream service"
        else:
            error_message = f"Error connecting to upstream service: {error_type}"

        return create_error_response("upstream_error", error_message, 502)

    except Exception as exc:
        await client.aclose()
        tb = traceback.format_exc()

        logger.error(
            "Unexpected error in upstream forwarding",
            extra={
                "error": str(exc),
                "error_type": type(exc).__name__,
                "method": request.method,
                "url": url,
                "path": path,
                "query_params": dict(request.query_params),
                "key_hash": key.hashed_key[:8] + "...",
                "traceback": tb,
            },
        )

        return create_error_response(
            "internal_error", "An unexpected server error occurred", 500
        )


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(
    request: Request, path: str, session: AsyncSession = Depends(get_session)
) -> Response | StreamingResponse:
    """Main proxy endpoint handler."""
    logger.info(
        "Received proxy request",
        extra={
            "method": request.method,
            "path": path,
            "client_host": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", "unknown")[:100],
        },
    )

    request_body = await request.body()
    headers = dict(request.headers)

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

    max_cost_for_model = get_cost_per_request(
        model=request_body_dict.get("model", None)
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
        return await x_cashu_handler(request, x_cashu, path)

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
        # Prepare headers for upstream (no model name for GET requests)
        headers = prepare_upstream_headers(dict(request.headers), None)
        return await forward_get_to_upstream(request, path, headers)

    cost_per_request = 0
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
            await pay_for_request(key, session, request_body_dict)
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
    model_name = None
    if request_body_dict:
        from .payment.helpers import extract_model_from_request

        model_name = extract_model_from_request(request_body_dict)
    headers = prepare_upstream_headers(dict(request.headers), model_name)

    # Forward to upstream and handle response
    response = await forward_to_upstream(
        request, path, headers, request_body, key, max_cost_for_model, session
    )

    if response.status_code != 200:
        await revert_pay_for_request(key, session, cost_per_request)
        logger.warning(
            "Upstream request failed, revert payment",
            extra={
                "status_code": response.status_code,
                "path": path,
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance": key.balance,
            },
        )

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


async def forward_get_to_upstream(
    request: Request,
    path: str,
    headers: dict,
) -> Response | StreamingResponse:
    """Forward request to upstream and handle the response."""
    # For GET requests, we don't have a request body, so use default URL
    url = get_upstream_url_for_request(path, None)

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
                    params=request.query_params,
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
                "internal_error", "An unexpected server error occurred", 500
            )
