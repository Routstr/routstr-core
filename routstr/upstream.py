import json
import re
import traceback
from typing import AsyncGenerator, TypedDict

import httpx
from fastapi import BackgroundTasks, Request, Response
from fastapi.responses import StreamingResponse

from .auth import adjust_payment_for_tokens
from .core import get_logger
from .core.db import ApiKey, AsyncSession, create_session
from .payment.helpers import create_error_response
from .request import RoutstrRequest

logger = get_logger(__name__)


class CostDict(TypedDict):
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


class UpstreamProvider:
    def __init__(self, url: str, api_key: str):
        self.url = url
        self.api_key = api_key

    def recommended_models(self) -> list[str]:
        return []

    def get_cost_from_response(self, response: httpx.Response) -> CostDict:
        return CostDict(
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
        )

    async def forward(
        self, request: RoutstrRequest, db_session: AsyncSession
    ) -> Response | StreamingResponse:
        headers = self.prepare_upstream_headers(request.request_headers)

        response = await self.forward_to_upstream(
            request._request,
            request.path,
            headers,
            request.request_body,
            request._balance,
            request.reserved_balance,
            db_session,
        )
        return response

    async def handle_streaming_chat_completion(
        self, response: httpx.Response, key: ApiKey, max_cost_for_model: int
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

        async def stream_with_cost(
            max_cost_for_model: int,
        ) -> AsyncGenerator[bytes, None]:
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
                                                    "key_hash": key.hashed_key[:8]
                                                    + "...",
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

        return StreamingResponse(
            stream_with_cost(max_cost_for_model),
            status_code=response.status_code,
            headers=dict(response.headers),
        )

    async def handle_non_streaming_chat_completion(
        self,
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
                k: v
                for k, v in response.headers.items()
                if k.lower() in allowed_headers
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
        self,
        request: Request,
        path: str,
        headers: dict,
        request_body: bytes | None,
        key: ApiKey,
        max_cost_for_model: int,
        session: AsyncSession,
    ) -> Response | StreamingResponse:
        """Forward request to upstream and handle the response."""
        if path.startswith("v1/"):
            path = path.replace("v1/", "")

        url = f"{self.url}/{path}"

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
                    result = await self.handle_streaming_chat_completion(
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
                        return await self.handle_non_streaming_chat_completion(
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

            return create_error_response(
                "upstream_error", error_message, 502, request=request
            )

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
                "internal_error",
                "An unexpected server error occurred",
                500,
                request=request,
            )

    def prepare_upstream_headers(self, request_headers: dict) -> dict:
        """Prepare headers for upstream request, removing sensitive/problematic ones."""
        logger.debug(
            "Preparing upstream headers",
            extra={
                "original_headers_count": len(request_headers),
                "has_upstream_api_key": bool(self.api_key),
            },
        )

        headers = dict(request_headers)

        # Remove headers that shouldn't be forwarded
        removed_headers = []
        for header in [
            "host",
            "content-length",
            "refund-lnurl",
            "key-expiry-time",
            "x-cashu",
        ]:
            if headers.pop(header, None) is not None:
                removed_headers.append(header)

        # Handle authorization
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            if headers.pop("authorization", None) is not None:
                removed_headers.append("authorization (replaced with upstream key)")
        else:
            for auth_header in ["Authorization", "authorization"]:
                if headers.pop(auth_header, None) is not None:
                    removed_headers.append(auth_header)

        logger.debug(
            "Headers prepared for upstream",
            extra={
                "final_headers_count": len(headers),
                "removed_headers": removed_headers,
                "added_upstream_auth": bool(self.api_key),
            },
        )

        return headers
