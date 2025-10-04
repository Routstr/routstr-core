from __future__ import annotations

import json
import re
import traceback
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING, Mapping

import httpx

if TYPE_CHECKING:
    from .payment.cost_caculation import CostData, MaxCostData

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .auth import adjust_payment_for_tokens
from .core import get_logger
from .core.db import ApiKey, AsyncSession, create_session
from .core.settings import settings
from .payment.helpers import create_error_response

logger = get_logger(__name__)


def prepare_upstream_headers(request_headers: dict) -> dict:
    upstream_api_key = settings.upstream_api_key
    logger.debug(
        "Preparing upstream headers",
        extra={
            "original_headers_count": len(request_headers),
            "has_upstream_api_key": bool(upstream_api_key),
        },
    )

    headers = dict(request_headers)
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

    if upstream_api_key:
        headers["Authorization"] = f"Bearer {upstream_api_key}"
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
            "added_upstream_auth": bool(upstream_api_key),
        },
    )

    return headers


def prepare_upstream_params(
    path: str, query_params: Mapping[str, str] | None
) -> dict[str, str]:
    params: dict[str, str] = dict(query_params or {})
    chat_api_version = settings.chat_completions_api_version
    if path.endswith("chat/completions") and chat_api_version:
        params["api-version"] = chat_api_version
    return params


def _extract_upstream_error_message(body_bytes: bytes) -> tuple[str, str | None]:
    message: str = "Upstream request failed"
    upstream_code: str | None = None
    if not body_bytes:
        return message, upstream_code
    try:
        data = json.loads(body_bytes)
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                raw_msg = err.get("message") or err.get("detail") or err.get("error")
                if isinstance(raw_msg, (str, int, float)):
                    message = str(raw_msg)
                upstream_code_raw = err.get("code") or err.get("type")
                if isinstance(upstream_code_raw, (str, int, float)):
                    upstream_code = str(upstream_code_raw)
            elif "message" in data and isinstance(data["message"], (str, int, float)):
                message = str(data["message"])  # type: ignore[arg-type]
            elif "detail" in data and isinstance(data["detail"], (str, int, float)):
                message = str(data["detail"])  # type: ignore[arg-type]
    except Exception:
        preview = body_bytes.decode("utf-8", errors="ignore").strip()
        if preview:
            message = preview[:500]
    return message, upstream_code


async def map_upstream_error_response(
    request: Request,
    path: str,
    upstream_response: httpx.Response,
) -> Response:
    status_code = upstream_response.status_code
    headers = dict(upstream_response.headers)
    content_type = headers.get("content-type", "")
    try:
        body_bytes = await upstream_response.aread()
    except Exception:
        body_bytes = b""

    message, upstream_code = _extract_upstream_error_message(body_bytes)
    lowered_message = message.lower()
    lowered_code = (upstream_code or "").lower()

    error_type = "upstream_error"
    mapped_status = 502

    if status_code in (400, 422):
        error_type = "invalid_request_error"
        mapped_status = 400
    elif status_code in (401, 403):
        error_type = "upstream_auth_error"
        mapped_status = 502
    elif status_code == 404:
        if path.endswith("chat/completions"):
            error_type = "invalid_model"
            mapped_status = 400
            if not message or message == "Upstream request failed":
                message = "Requested model is not available upstream"
        elif "model" in lowered_message or "model" in lowered_code:
            error_type = "invalid_model"
            mapped_status = 400
            if not message or message == "Upstream request failed":
                message = "Requested model is not available upstream"
        else:
            error_type = "upstream_error"
            mapped_status = 502
    elif status_code == 429:
        error_type = "rate_limit_exceeded"
        mapped_status = 429
    elif status_code >= 500:
        error_type = "upstream_error"
        mapped_status = 502

    logger.debug(
        "Mapped upstream error",
        extra={
            "path": path,
            "upstream_status": status_code,
            "mapped_status": mapped_status,
            "error_type": error_type,
            "upstream_content_type": content_type,
            "message_preview": message[:200],
        },
    )

    return create_error_response(error_type, message, mapped_status, request=request)


async def handle_streaming_chat_completion(
    response: httpx.Response, key: ApiKey, max_cost_for_model: int
) -> StreamingResponse:
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

            if not usage_finalized:
                maybe_cost_event = await finalize_without_usage()
                if maybe_cost_event is not None:
                    yield maybe_cost_event

        except Exception as stream_error:
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


async def handle_non_streaming_chat_completion(
    response: httpx.Response,
    key: ApiKey,
    session: AsyncSession,
    deducted_max_cost: int,
) -> Response:
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


class UpstreamProvider:
    def __init__(
        self, base_url: str, api_key: str, chat_completions_api_version: str = ""
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.chat_completions_api_version = chat_completions_api_version

    def prepare_headers(self, request_headers: dict) -> dict:
        logger.debug(
            "Preparing upstream headers",
            extra={
                "original_headers_count": len(request_headers),
                "has_upstream_api_key": bool(self.api_key),
            },
        )

        headers = dict(request_headers)
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

    def prepare_params(
        self, path: str, query_params: Mapping[str, str] | None
    ) -> dict[str, str]:
        params: dict[str, str] = dict(query_params or {})
        if path.endswith("chat/completions") and self.chat_completions_api_version:
            params["api-version"] = self.chat_completions_api_version
        return params

    async def forward_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        request_body: bytes | None,
        key: ApiKey,
        max_cost_for_model: int,
        session: AsyncSession,
        map_upstream_error_response: Callable[
            [Request, str, httpx.Response], Awaitable[Response]
        ],
        handle_streaming_chat_completion: Callable[
            [httpx.Response, ApiKey, int], Awaitable[StreamingResponse]
        ],
        handle_non_streaming_chat_completion: Callable[
            [httpx.Response, ApiKey, AsyncSession, int], Awaitable[Response]
        ],
    ) -> Response | StreamingResponse:
        if path.startswith("v1/"):
            path = path.replace("v1/", "")

        url = f"{self.base_url}/{path}"

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
            timeout=None,
        )

        try:
            if request_body is not None:
                response = await client.send(
                    client.build_request(
                        request.method,
                        url,
                        headers=headers,
                        content=request_body,
                        params=self.prepare_params(path, request.query_params),
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
                        params=self.prepare_params(path, request.query_params),
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

            if response.status_code != 200:
                try:
                    mapped_error = await map_upstream_error_response(
                        request, path, response
                    )
                finally:
                    await response.aclose()
                    await client.aclose()
                return mapped_error

            if path.endswith("chat/completions"):
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
                    result = await handle_streaming_chat_completion(
                        response, key, max_cost_for_model
                    )
                    background_tasks = BackgroundTasks()
                    background_tasks.add_task(response.aclose)
                    background_tasks.add_task(client.aclose)
                    result.background = background_tasks
                    return result

                elif response.status_code == 200:
                    try:
                        return await handle_non_streaming_chat_completion(
                            response, key, session, max_cost_for_model
                        )
                    finally:
                        await response.aclose()
                        await client.aclose()

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

    async def forward_get_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        map_upstream_error_response: Callable[
            [Request, str, httpx.Response], Awaitable[Response]
        ],
    ) -> Response | StreamingResponse:
        if path.startswith("v1/"):
            path = path.replace("v1/", "")

        url = f"{self.base_url}/{path}"

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
                        params=self.prepare_params(path, request.query_params),
                    ),
                )

                logger.info(
                    "GET request forwarded successfully",
                    extra={"path": path, "status_code": response.status_code},
                )
                if response.status_code != 200:
                    try:
                        mapped = await map_upstream_error_response(
                            request, path, response
                        )
                    finally:
                        await response.aclose()
                    return mapped

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

    async def forward_x_cashu_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        amount: int,
        unit: str,
        max_cost_for_model: int,
        handle_x_cashu_chat_completion: Callable[
            [httpx.Response, int, str, int], Awaitable[Response | StreamingResponse]
        ],
        send_refund: Callable[[int, str], Awaitable[str]],
    ) -> Response | StreamingResponse:
        if path.startswith("v1/"):
            path = path.replace("v1/", "")

        url = f"{self.base_url}/{path}"

        logger.debug(
            "Forwarding request to upstream",
            extra={
                "url": url,
                "method": request.method,
                "path": path,
                "amount": amount,
                "unit": unit,
            },
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
                        params=self.prepare_params(path, request.query_params),
                    ),
                    stream=True,
                )

                logger.debug(
                    "Received upstream response",
                    extra={
                        "status_code": response.status_code,
                        "path": path,
                        "response_headers": dict(response.headers),
                    },
                )

                if response.status_code != 200:
                    logger.warning(
                        "Upstream request failed, processing refund",
                        extra={
                            "status_code": response.status_code,
                            "path": path,
                            "amount": amount,
                            "unit": unit,
                        },
                    )

                    refund_token = await send_refund(amount - 60, unit)

                    logger.info(
                        "Refund processed for failed upstream request",
                        extra={
                            "status_code": response.status_code,
                            "refund_amount": amount,
                            "unit": unit,
                            "refund_token_preview": refund_token[:20] + "..."
                            if len(refund_token) > 20
                            else refund_token,
                        },
                    )

                    error_response = Response(
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
                    error_response.headers["X-Cashu"] = refund_token
                    return error_response

                if path.endswith("chat/completions"):
                    logger.debug(
                        "Processing chat completion response",
                        extra={"path": path, "amount": amount, "unit": unit},
                    )

                    result = await handle_x_cashu_chat_completion(
                        response, amount, unit, max_cost_for_model
                    )
                    background_tasks = BackgroundTasks()
                    background_tasks.add_task(response.aclose)
                    result.background = background_tasks
                    return result

                background_tasks = BackgroundTasks()
                background_tasks.add_task(response.aclose)
                background_tasks.add_task(client.aclose)

                logger.debug(
                    "Streaming non-chat response",
                    extra={"path": path, "status_code": response.status_code},
                )

                return StreamingResponse(
                    response.aiter_bytes(),
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    background=background_tasks,
                )
            except Exception as exc:
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
                        "traceback": tb,
                    },
                )
                return create_error_response(
                    "internal_error",
                    "An unexpected server error occurred",
                    500,
                    request=request,
                )

    async def handle_x_cashu(
        self, request: Request, x_cashu_token: str, path: str, max_cost_for_model: int
    ) -> Response | StreamingResponse:
        from .wallet import recieve_token

        logger.info(
            "Processing X-Cashu payment request",
            extra={
                "path": path,
                "method": request.method,
                "token_preview": x_cashu_token[:20] + "..."
                if len(x_cashu_token) > 20
                else x_cashu_token,
            },
        )

        try:
            headers = dict(request.headers)
            amount, unit, mint = await recieve_token(x_cashu_token)
            headers = self.prepare_headers(dict(request.headers))

            logger.info(
                "X-Cashu token redeemed successfully",
                extra={"amount": amount, "unit": unit, "path": path, "mint": mint},
            )

            return await self.forward_x_cashu_request(
                request,
                path,
                headers,
                amount,
                unit,
                max_cost_for_model,
                handle_x_cashu_chat_completion,
                send_refund,
            )
        except Exception as e:
            error_message = str(e)
            logger.error(
                "X-Cashu payment request failed",
                extra={
                    "error": error_message,
                    "error_type": type(e).__name__,
                    "path": path,
                    "method": request.method,
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
                "cashu_error",
                f"CASHU token processing failed: {error_message}",
                400,
                request=request,
                token=x_cashu_token,
            )


async def handle_x_cashu_chat_completion(
    response: httpx.Response, amount: int, unit: str, max_cost_for_model: int
) -> StreamingResponse | Response:
    logger.debug(
        "Handling chat completion response",
        extra={"amount": amount, "unit": unit, "status_code": response.status_code},
    )

    try:
        content = await response.aread()
        content_str = content.decode("utf-8") if isinstance(content, bytes) else content
        is_streaming = content_str.startswith("data:") or "data:" in content_str

        logger.debug(
            "Chat completion response analysis",
            extra={
                "is_streaming": is_streaming,
                "content_length": len(content_str),
                "amount": amount,
                "unit": unit,
            },
        )

        if is_streaming:
            return await handle_x_cashu_streaming_response(
                content_str, response, amount, unit, max_cost_for_model
            )
        else:
            return await handle_x_cashu_non_streaming_response(
                content_str, response, amount, unit, max_cost_for_model
            )

    except Exception as e:
        logger.error(
            "Error processing chat completion response",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "amount": amount,
                "unit": unit,
            },
        )
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=dict(response.headers),
        )


async def handle_x_cashu_streaming_response(
    content_str: str,
    response: httpx.Response,
    amount: int,
    unit: str,
    max_cost_for_model: int,
) -> StreamingResponse:
    logger.debug(
        "Processing streaming response",
        extra={
            "amount": amount,
            "unit": unit,
            "content_lines": len(content_str.strip().split("\n")),
        },
    )

    response_headers = dict(response.headers)
    if "transfer-encoding" in response_headers:
        del response_headers["transfer-encoding"]
    if "content-encoding" in response_headers:
        del response_headers["content-encoding"]

    usage_data = None
    model = None

    lines = content_str.strip().split("\n")
    for line in lines:
        if line.startswith("data: "):
            try:
                data_json = json.loads(line[6:])
                if "usage" in data_json:
                    usage_data = data_json["usage"]
                    model = data_json.get("model")
                elif "model" in data_json and not model:
                    model = data_json["model"]
            except json.JSONDecodeError:
                continue

    if usage_data and model:
        logger.debug(
            "Found usage data in streaming response",
            extra={
                "model": model,
                "usage_data": usage_data,
                "amount": amount,
                "unit": unit,
            },
        )

        response_data = {"usage": usage_data, "model": model}
        try:
            cost_data = await get_x_cashu_cost(response_data, max_cost_for_model)
            if cost_data:
                if unit == "msat":
                    refund_amount = amount - cost_data.total_msats
                elif unit == "sat":
                    refund_amount = amount - (cost_data.total_msats + 999) // 1000
                else:
                    raise ValueError(f"Invalid unit: {unit}")

                if refund_amount > 0:
                    logger.info(
                        "Processing refund for streaming response",
                        extra={
                            "original_amount": amount,
                            "cost_msats": cost_data.total_msats,
                            "refund_amount": refund_amount,
                            "unit": unit,
                            "model": model,
                        },
                    )

                    refund_token = await send_refund(refund_amount, unit)
                    response_headers["X-Cashu"] = refund_token

                    logger.info(
                        "Refund processed for streaming response",
                        extra={
                            "refund_amount": refund_amount,
                            "unit": unit,
                            "refund_token_preview": refund_token[:20] + "..."
                            if len(refund_token) > 20
                            else refund_token,
                        },
                    )
                else:
                    logger.debug(
                        "No refund needed for streaming response",
                        extra={
                            "amount": amount,
                            "cost_msats": cost_data.total_msats,
                            "model": model,
                        },
                    )
        except Exception as e:
            logger.error(
                "Error calculating cost for streaming response",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "model": model,
                    "amount": amount,
                    "unit": unit,
                },
            )

    async def generate() -> AsyncGenerator[bytes, None]:
        for line in lines:
            yield (line + "\n").encode("utf-8")

    return StreamingResponse(
        generate(),
        status_code=response.status_code,
        headers=response_headers,
        media_type="text/plain",
    )


async def handle_x_cashu_non_streaming_response(
    content_str: str,
    response: httpx.Response,
    amount: int,
    unit: str,
    max_cost_for_model: int,
) -> Response:
    logger.debug(
        "Processing non-streaming response",
        extra={"amount": amount, "unit": unit, "content_length": len(content_str)},
    )

    try:
        response_json = json.loads(content_str)
        cost_data = await get_x_cashu_cost(response_json, max_cost_for_model)

        if not cost_data:
            logger.error(
                "Failed to calculate cost for response",
                extra={
                    "amount": amount,
                    "unit": unit,
                    "response_model": response_json.get("model", "unknown"),
                },
            )
            return Response(
                content=json.dumps(
                    {
                        "error": {
                            "message": "Error forwarding request to upstream",
                            "type": "upstream_error",
                            "code": response.status_code,
                        }
                    }
                ),
                status_code=response.status_code,
                media_type="application/json",
            )

        response_headers = dict(response.headers)
        if "transfer-encoding" in response_headers:
            del response_headers["transfer-encoding"]
        if "content-encoding" in response_headers:
            del response_headers["content-encoding"]

        if unit == "msat":
            refund_amount = amount - cost_data.total_msats
        elif unit == "sat":
            refund_amount = amount - (cost_data.total_msats + 999) // 1000
        else:
            raise ValueError(f"Invalid unit: {unit}")

        logger.info(
            "Processing non-streaming response cost calculation",
            extra={
                "original_amount": amount,
                "cost_msats": cost_data.total_msats,
                "refund_amount": refund_amount,
                "unit": unit,
                "model": response_json.get("model", "unknown"),
            },
        )

        if refund_amount > 0:
            refund_token = await send_refund(refund_amount, unit)
            response_headers["X-Cashu"] = refund_token

            logger.info(
                "Refund processed for non-streaming response",
                extra={
                    "refund_amount": refund_amount,
                    "unit": unit,
                    "refund_token_preview": refund_token[:20] + "..."
                    if len(refund_token) > 20
                    else refund_token,
                },
            )

        return Response(
            content=content_str,
            status_code=response.status_code,
            headers=response_headers,
            media_type="application/json",
        )
    except json.JSONDecodeError as e:
        logger.error(
            "Failed to parse JSON from upstream response",
            extra={
                "error": str(e),
                "content_preview": content_str[:200] + "..."
                if len(content_str) > 200
                else content_str,
                "amount": amount,
                "unit": unit,
            },
        )

        from .wallet import send_token

        emergency_refund = amount
        refund_token = await send_token(emergency_refund, unit=unit)
        response.headers["X-Cashu"] = refund_token

        logger.warning(
            "Emergency refund issued due to JSON parse error",
            extra={
                "original_amount": amount,
                "refund_amount": emergency_refund,
                "deduction": 60,
            },
        )

        return Response(
            content=content_str,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type="application/json",
        )


async def get_x_cashu_cost(
    response_data: dict, max_cost_for_model: int
) -> MaxCostData | CostData | None:
    from .payment.cost_caculation import (
        CostData,
        CostDataError,
        MaxCostData,
        calculate_cost,
    )

    model = response_data.get("model", None)
    logger.debug(
        "Calculating cost for response",
        extra={"model": model, "has_usage": "usage" in response_data},
    )

    async with create_session() as session:
        match await calculate_cost(response_data, max_cost_for_model, session):
            case MaxCostData() as cost:
                logger.debug(
                    "Using max cost pricing",
                    extra={"model": model, "max_cost_msats": cost.total_msats},
                )
                return cost
            case CostData() as cost:
                logger.debug(
                    "Using token-based pricing",
                    extra={
                        "model": model,
                        "total_cost_msats": cost.total_msats,
                        "input_msats": cost.input_msats,
                        "output_msats": cost.output_msats,
                    },
                )
                return cost
            case CostDataError() as error:
                logger.error(
                    "Cost calculation error",
                    extra={
                        "model": model,
                        "error_message": error.message,
                        "error_code": error.code,
                    },
                )
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": {
                            "message": error.message,
                            "type": "invalid_request_error",
                            "code": error.code,
                        }
                    },
                )
    return None


async def send_refund(amount: int, unit: str, mint: str | None = None) -> str:
    from .wallet import send_token

    logger.debug(
        "Creating refund token", extra={"amount": amount, "unit": unit, "mint": mint}
    )

    max_retries = 3
    last_exception = None

    for attempt in range(max_retries):
        try:
            refund_token = await send_token(amount, unit=unit, mint_url=mint)

            logger.info(
                "Refund token created successfully",
                extra={
                    "amount": amount,
                    "unit": unit,
                    "mint": mint,
                    "attempt": attempt + 1,
                    "token_preview": refund_token[:20] + "..."
                    if len(refund_token) > 20
                    else refund_token,
                },
            )

            return refund_token
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                logger.warning(
                    "Refund token creation failed, retrying",
                    extra={
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "amount": amount,
                        "unit": unit,
                        "mint": mint,
                    },
                )
            else:
                logger.error(
                    "Failed to create refund token after all retries",
                    extra={
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "amount": amount,
                        "unit": unit,
                        "mint": mint,
                    },
                )

    raise HTTPException(
        status_code=401,
        detail={
            "error": {
                "message": f"failed to create refund after {max_retries} attempts: {str(last_exception)}",
                "type": "invalid_request_error",
                "code": "send_token_failed",
            }
        },
    )
