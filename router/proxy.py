import json
import os
import re
import traceback
from typing import AsyncGenerator

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .auth import (
    adjust_payment_for_tokens,
    check_token_balance,
    pay_for_request,
    validate_bearer_key,
)
from .cashu import x_cashu_refund
from .db import ApiKey, AsyncSession, create_session, get_session

UPSTREAM_BASE_URL = os.environ["UPSTREAM_BASE_URL"]
UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "")

proxy_router = APIRouter()


def prepare_upstream_headers(request_headers: dict) -> dict:
    """Prepare headers for upstream request, removing sensitive/problematic ones."""
    headers = dict(request_headers)
    # Remove headers that shouldn't be forwarded
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers.pop("refund-lnurl", None)
    headers.pop("key-expiry-time", None)
    headers.pop("x-cashu", None)

    # Handle authorization
    if UPSTREAM_API_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"
        headers.pop("authorization", None)
    else:
        headers.pop("Authorization", None)
        headers.pop("authorization", None)

    return headers


def create_error_response(error_type: str, message: str, status_code: int) -> Response:
    """Create a standardized error response."""
    return Response(
        content=json.dumps(
            {
                "error": {
                    "message": message,
                    "type": error_type,
                    "code": status_code,
                }
            }
        ),
        status_code=status_code,
        media_type="application/json",
    )


async def handle_streaming_chat_completion(
    response: httpx.Response, key: ApiKey, session: AsyncSession
) -> StreamingResponse:
    """Handle streaming chat completion responses with token-based pricing."""

    async def stream_with_cost() -> AsyncGenerator[bytes, None]:
        # Store all chunks to analyze
        stored_chunks = []

        async for chunk in response.aiter_bytes():
            # Store chunk for later analysis
            stored_chunks.append(chunk)

            # Pass through each chunk to client
            yield chunk

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
                            # Found usage data, calculate cost
                            # Create a new session for this operation
                            async with create_session() as new_session:
                                # Re-fetch the key in the new session
                                fresh_key = await new_session.get(
                                    key.__class__, key.hashed_key
                                )
                                if fresh_key:
                                    cost_data = await adjust_payment_for_tokens(
                                        fresh_key, data, new_session
                                    )
                                    # Format as SSE and yield
                                    cost_json = json.dumps({"cost": cost_data})
                                    yield f"data: {cost_json}\n\n".encode()
                            break
                    except json.JSONDecodeError:
                        continue

            except Exception as e:
                print(f"Error processing streaming response for cost: {e}")

    return StreamingResponse(
        stream_with_cost(),
        status_code=response.status_code,
        headers=dict(response.headers),
    )


async def handle_non_streaming_chat_completion(
    response: httpx.Response, key: ApiKey, session: AsyncSession
) -> Response:
    """Handle non-streaming chat completion responses with token-based pricing."""
    try:
        content = await response.aread()
        response_json = json.loads(content)
        cost_data = await adjust_payment_for_tokens(key, response_json, session)
        response_json["cost"] = cost_data

        response_headers = dict(response.headers)

        # Remove Transfer-Encoding header to avoid conflict with Content-Length header in common nginx setups
        if "transfer-encoding" in response_headers:
            del response_headers["transfer-encoding"]

        # Remove Content-Encoding header since we're sending uncompressed JSON
        if "content-encoding" in response_headers:
            del response_headers["content-encoding"]

        return Response(
            content=json.dumps(response_json).encode(),
            status_code=response.status_code,
            headers=response_headers,
            media_type="application/json",
        )
    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON from upstream response: {e}")
        raise
    except Exception as e:
        print(f"Error adjusting payment for tokens: {e}")
        raise


async def forward_to_upstream(
    request: Request,
    path: str,
    headers: dict,
    request_body: bytes | None,
    key: ApiKey,
    session: AsyncSession,
) -> Response | StreamingResponse:
    """Forward request to upstream and handle the response."""
    if path.startswith("v1/"):
        path = path.replace("v1/", "")

    url = f"{UPSTREAM_BASE_URL}/{path}"
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

        # For chat completions, we need to handle token-based pricing
        if path.endswith("chat/completions"):
            # Handle both streaming and non-streaming responses
            content_type = response.headers.get("content-type", "")
            is_streaming = "text/event-stream" in content_type

            if is_streaming and response.status_code == 200:
                # Process streaming response and extract cost from the last chunk
                result = await handle_streaming_chat_completion(response, key, session)
                background_tasks = BackgroundTasks()
                background_tasks.add_task(response.aclose)
                background_tasks.add_task(client.aclose)
                result.background = background_tasks
                return result

            elif response.status_code == 200 and "application/json" in content_type:
                # Handle non-streaming response
                try:
                    return await handle_non_streaming_chat_completion(
                        response, key, session
                    )
                finally:
                    await response.aclose()
                    await client.aclose()

        # For all other responses, stream the response
        background_tasks = BackgroundTasks()
        background_tasks.add_task(response.aclose)
        background_tasks.add_task(client.aclose)

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
        print(
            f"Error forwarding request to upstream: {error_type}: {error_details}\n"
            f"Request details: method={request.method}, url={url}, headers={headers}, "
            f"path={path}, query_params={dict(request.query_params)}"
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
        import traceback

        tb = traceback.format_exc()
        print(
            f"Unexpected error: {exc}\n"
            f"Request details: method={request.method}, url={url}, headers={headers}, "
            f"path={path}, query_params={dict(request.query_params)}\n"
            f"Traceback:\n{tb}"
        )
        return create_error_response(
            "internal_error", "An unexpected server error occurred", 500
        )


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(
    request: Request, path: str, session: AsyncSession = Depends(get_session)
) -> Response | StreamingResponse:
    request_body = await request.body()
    headers = dict(request.headers)

    # Parse JSON body if present, handle empty/invalid JSON
    request_body_dict = {}
    if request_body:
        try:
            request_body_dict = json.loads(request_body)
        except json.JSONDecodeError:
            return Response(
                content=json.dumps(
                    {"error": {"type": "invalid_request_error", "code": "invalid_json"}}
                ),
                status_code=400,
                media_type="application/json",
            )

    # Handle authentication
    if x_cashu := headers.get("x-cashu", None):
        # Check token balance before authentication for cashu tokens
        if request_body_dict:
            check_token_balance(headers, request_body_dict)
        key = await validate_bearer_key(x_cashu, session, "X-CASHU")

    elif auth := headers.get("authorization", None):
        key = await get_bearer_token_key(headers, path, session, auth)

    else:
        if request.method not in ["GET"]:
            return Response(
                content=json.dumps({"detail": "Unauthorized"}),
                status_code=401,
                media_type="application/json",
            )

        # Prepare headers for upstream
        headers = prepare_upstream_headers(dict(request.headers))
        return await forward_get_to_upstream(request, path, headers)

    # Only pay for request if we have request body data (for completions endpoints)
    if request_body_dict:
        await pay_for_request(key, session, request_body_dict)

    # Prepare headers for upstream
    headers = prepare_upstream_headers(dict(request.headers))

    # Forward to upstream and handle response
    response = await forward_to_upstream(
        request, path, headers, request_body, key, session
    )

    if response.status_code != 200 and key.refund_address == "X-CASHU":
        print(key)
        refund_token = await x_cashu_refund(key, session)
        response = Response(
            content=json.dumps(
                {
                    "error": {
                        "message": "Error forwarding request to upstream",
                        "type": "upstream_error",
                        "code": response.status_code,
                        "refund_token": refund_token,
                    }
                }
            ),
            status_code=response.status_code,
            media_type="application/json",
        )
        response.headers["X-Cashu"] = refund_token
        return response

    if key is not None and key.refund_address == "X-CASHU":
        refund_token = await x_cashu_refund(key, session)
        response.headers["X-Cashu"] = refund_token

    return response


async def get_bearer_token_key(
    headers: dict, path: str, session: AsyncSession, auth: str
) -> ApiKey:
    """Handle bearer token authentication proxy requests."""
    bearer_key = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    refund_address = headers.get("Refund-LNURL", None)
    key_expiry_time = headers.get("Key-Expiry-Time", None)

    # Validate key_expiry_time header
    if key_expiry_time:
        try:
            key_expiry_time = int(key_expiry_time)  # type: ignore
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid Key-Expiry-Time: must be a valid Unix timestamp",
            )
        if not refund_address:
            raise HTTPException(
                status_code=400,
                detail="Error: Refund-LNURL header required when using Key-Expiry-Time",
            )
    else:
        key_expiry_time = None

    return await validate_bearer_key(
        bearer_key,
        session,
        refund_address,
        key_expiry_time,  # type: ignore
    )


async def forward_get_to_upstream(
    request: Request,
    path: str,
    headers: dict,
) -> Response | StreamingResponse:
    """Forward request to upstream and handle the response."""
    if path.startswith("v1/"):
        path = path.replace("v1/", "")

    url = f"{UPSTREAM_BASE_URL}/{path}"

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

            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                headers=dict(response.headers),
            )
        except Exception as exc:
            tb = traceback.format_exc()
            print(
                f"Unexpected error: {exc}\n"
                f"Request details: method={request.method}, url={url}, headers={headers}, "
                f"path={path}, query_params={dict(request.query_params)}\n"
                f"Traceback:\n{tb}"
            )
            return create_error_response(
                "internal_error", "An unexpected server error occurred", 500
            )
