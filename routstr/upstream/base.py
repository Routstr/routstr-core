"""Refactored BaseUpstreamProvider with improved code organization and reusability."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Mapping

import httpx
from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from ..auth import adjust_payment_for_tokens
from ..core import get_logger
from ..core.db import ApiKey, AsyncSession, create_session

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

from ..payment.cost_calculation import (
    CostData,
    CostDataError,
    MaxCostData,
    calculate_cost,
)
from ..payment.helpers import create_error_response
from ..payment.models import (
    Model,
    Pricing,
    _calculate_usd_max_costs,
    _update_model_sats_pricing,
)
from ..payment.price import sats_usd_price

# Import our new modular components
from .handlers.cashu_payment_handler import (
    ChatCompletionCashuHandler,
    ResponsesApiCashuHandler,
)
from .processing.http_client import HttpForwarder, StreamingResponseWrapper

logger = get_logger(__name__)


class TopupData(BaseModel):
    """Universal top-up data schema for Lightning Network invoices."""

    invoice_id: str
    payment_request: str
    amount: int
    currency: str
    expires_at: int | None = None
    checkout_url: str | None = None


class BaseUpstreamProvider:
    """Provider for forwarding requests to an upstream AI service API."""

    provider_type: str = "base"
    default_base_url: str | None = None
    platform_url: str | None = None

    base_url: str
    api_key: str
    provider_fee: float = 1.05
    _models_cache: list[Model] = []
    _models_by_id: dict[str, Model] = {}

    def __init__(self, base_url: str, api_key: str, provider_fee: float = 1.01):
        """Initialize the upstream provider."""
        self.base_url = base_url
        self.api_key = api_key
        self.provider_fee = provider_fee
        self._models_cache = []
        self._models_by_id = {}

        # Initialize modular components
        self.http_forwarder = HttpForwarder(base_url)
        self.chat_cashu_handler = ChatCompletionCashuHandler(base_url)
        self.responses_cashu_handler = ResponsesApiCashuHandler(base_url)
        self.streaming_wrapper = StreamingResponseWrapper()

    @classmethod
    def from_db_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "BaseUpstreamProvider | None":
        """Factory method to instantiate provider from database row."""
        return cls(
            base_url=provider_row.base_url,
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        """Get metadata about this provider type for API responses."""
        return {
            "id": cls.provider_type,
            "name": cls.provider_type.title(),
            "default_base_url": cls.default_base_url or "",
            "fixed_base_url": bool(cls.default_base_url),
            "platform_url": cls.platform_url,
            "can_create_account": False,
            "can_topup": False,
            "can_show_balance": False,
        }

    def prepare_headers(self, request_headers: dict) -> dict:
        """Prepare headers for upstream request by removing proxy-specific headers and adding authentication."""
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

        for header in ["authorization", "accept-encoding"]:
            if headers.pop(header, None) is not None:
                removed_headers.append(f"{header} (replaced with routstr-safe version)")

        headers["accept-encoding"] = "gzip, deflate, br, identity"

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
    ) -> Mapping[str, str]:
        """Prepare query parameters for upstream request."""
        return query_params or {}

    def transform_model_name(self, model_id: str) -> str:
        """Transform model ID for this provider's API format."""
        return model_id

    def prepare_responses_request_body(
        self, body: bytes | None, model_obj: Model
    ) -> bytes | None:
        """Transform request body for Responses API specific requirements."""
        if not body:
            return body

        try:
            data = json.loads(body)
            if isinstance(data, dict):
                if "model" in data:
                    original_model = model_obj.id
                    transformed_model = self.transform_model_name(original_model)
                    data["model"] = transformed_model

                    logger.debug(
                        "Transformed model name in Responses API request",
                        extra={
                            "original": original_model,
                            "transformed": transformed_model,
                            "provider": self.provider_type or self.base_url,
                        },
                    )

                if "input" in data and isinstance(data["input"], dict) and "model" in data["input"]:
                    original_model = model_obj.id
                    transformed_model = self.transform_model_name(original_model)
                    data["input"]["model"] = transformed_model

                return json.dumps(data).encode()
        except Exception as e:
            logger.debug(
                "Could not transform Responses API request body",
                extra={
                    "error": str(e),
                    "provider": self.provider_type or self.base_url,
                },
            )

        return body

    def prepare_request_body(
        self, body: bytes | None, model_obj: Model
    ) -> bytes | None:
        """Transform request body for provider-specific requirements."""
        if not body:
            return body

        try:
            data = json.loads(body)
            if isinstance(data, dict) and "model" in data:
                original_model = model_obj.id
                transformed_model = self.transform_model_name(original_model)
                data["model"] = transformed_model
                logger.debug(
                    "Transformed model name in request",
                    extra={
                        "original": original_model,
                        "transformed": transformed_model,
                        "provider": self.provider_type or self.base_url,
                    },
                )
                return json.dumps(data).encode()
        except Exception as e:
            logger.debug(
                "Could not transform request body",
                extra={
                    "error": str(e),
                    "provider": self.provider_type or self.base_url,
                },
            )

        return body

    def _extract_upstream_error_message(
        self, body_bytes: bytes
    ) -> tuple[str, str | None]:
        """Extract error message and code from upstream error response body."""
        message: str = "Upstream request failed"
        upstream_code: str | None = None
        if not body_bytes:
            return message, upstream_code
        try:
            data = json.loads(body_bytes)
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    raw_msg = (
                        err.get("message") or err.get("detail") or err.get("error")
                    )
                    if isinstance(raw_msg, (str, int, float)):
                        message = str(raw_msg)
                    upstream_code_raw = err.get("code") or err.get("type")
                    if isinstance(upstream_code_raw, (str, int, float)):
                        upstream_code = str(upstream_code_raw)
                elif "message" in data and isinstance(
                    data["message"], (str, int, float)
                ):
                    message = str(data["message"])
                elif "detail" in data and isinstance(data["detail"], (str, int, float)):
                    message = str(data["detail"])
        except Exception:
            preview = body_bytes.decode("utf-8", errors="ignore").strip()
            if preview:
                message = preview[:500]
        return message, upstream_code

    async def map_upstream_error_response(
        self, request: Request, path: str, upstream_response: httpx.Response
    ) -> Response:
        """Map upstream error responses to appropriate proxy error responses."""
        status_code = upstream_response.status_code
        headers = dict(upstream_response.headers)
        content_type = headers.get("content-type", "")
        try:
            body_bytes = await upstream_response.aread()
        except Exception:
            body_bytes = b""

        message, upstream_code = self._extract_upstream_error_message(body_bytes)
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

        return create_error_response(
            error_type, message, mapped_status, request=request
        )

    async def handle_streaming_chat_completion(
        self, response: httpx.Response, key: ApiKey, max_cost_for_model: int
    ) -> StreamingResponse:
        """Handle streaming chat completion responses with token usage tracking and cost adjustment."""
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
                                                cost_data = (
                                                    await adjust_payment_for_tokens(
                                                        fresh_key,
                                                        data,
                                                        new_session,
                                                        max_cost_for_model,
                                                    )
                                                )
                                                usage_finalized = True
                                                logger.info(
                                                    "Payment adjustment completed for streaming",
                                                    extra={
                                                        "key_hash": key.hashed_key[:8]
                                                        + "...",
                                                        "cost_data": cost_data,
                                                        "model": last_model_seen,
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

        response_headers = dict(response.headers)
        response_headers.pop("content-encoding", None)
        response_headers.pop("content-length", None)

        return StreamingResponse(
            stream_with_cost(max_cost_for_model),
            status_code=response.status_code,
            headers=response_headers,
        )

    async def handle_non_streaming_chat_completion(
        self,
        response: httpx.Response,
        key: ApiKey,
        session: AsyncSession,
        deducted_max_cost: int,
    ) -> Response:
        """Handle non-streaming chat completion responses with token usage tracking and cost adjustment."""
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
                "Payment adjustment completed for non-streaming",
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

    async def handle_streaming_responses_completion(
        self, response: httpx.Response, key: ApiKey, max_cost_for_model: int
    ) -> StreamingResponse:
        """Handle streaming Responses API responses with token usage tracking and cost adjustment."""
        logger.info(
            "Processing streaming Responses API completion",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance": key.balance,
                "response_status": response.status_code,
            },
        )

        async def stream_with_responses_cost(
            max_cost_for_model: int,
        ) -> AsyncGenerator[bytes, None]:
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
                            "Finalized Responses API streaming payment without explicit usage",
                            extra={
                                "key_hash": key.hashed_key[:8] + "...",
                                "cost_data": cost_data,
                                "balance_after_adjustment": fresh_key.balance,
                            },
                        )
                        return f"data: {json.dumps({'cost': cost_data})}\n\n".encode()
                    except Exception as cost_error:
                        logger.error(
                            "Error finalizing Responses API payment without usage",
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
                                if isinstance(obj, dict):
                                    if obj.get("model"):
                                        last_model_seen = str(obj.get("model"))
                            except json.JSONDecodeError:
                                pass
                    except Exception:
                        pass

                    yield chunk

                logger.debug(
                    "Responses API streaming completed, analyzing usage data",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "chunks_count": len(stored_chunks),
                    },
                )

                # Process final usage data
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
                                                cost_data = (
                                                    await adjust_payment_for_tokens(
                                                        fresh_key,
                                                        data,
                                                        new_session,
                                                        max_cost_for_model,
                                                    )
                                                )
                                                usage_finalized = True
                                                logger.info(
                                                    "Payment adjustment completed for Responses API streaming",
                                                    extra={
                                                        "key_hash": key.hashed_key[:8]
                                                        + "...",
                                                        "cost_data": cost_data,
                                                        "model": last_model_seen,
                                                        "balance_after_adjustment": fresh_key.balance,
                                                    },
                                                )
                                                yield f"data: {json.dumps({'cost': cost_data})}\n\n".encode()
                                            except Exception as cost_error:
                                                logger.error(
                                                    "Error adjusting payment for Responses API streaming tokens",
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
                            "Error processing Responses API streaming response chunk",
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
                    "Responses API streaming interrupted; finalizing without usage",
                    extra={
                        "error": str(stream_error),
                        "error_type": type(stream_error).__name__,
                        "key_hash": key.hashed_key[:8] + "...",
                    },
                )
                await finalize_without_usage()
                raise

        response_headers = dict(response.headers)
        response_headers.pop("content-encoding", None)
        response_headers.pop("content-length", None)

        return StreamingResponse(
            stream_with_responses_cost(max_cost_for_model),
            status_code=response.status_code,
            headers=response_headers,
        )

    async def handle_non_streaming_responses_completion(
        self,
        response: httpx.Response,
        key: ApiKey,
        session: AsyncSession,
        deducted_max_cost: int,
    ) -> Response:
        """Handle non-streaming Responses API responses with token usage tracking and cost adjustment."""
        try:
            content = await response.aread()
            response_json = json.loads(content)

            logger.debug(
                "Parsed Responses API response JSON",
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
                "Payment adjustment completed for non-streaming Responses API",
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
                "Failed to parse JSON from upstream Responses API response",
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
                "Error processing non-streaming Responses API completion",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )
            raise

    async def forward_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        request_body: bytes | None,
        key: ApiKey,
        max_cost_for_model: int,
        session: AsyncSession,
        model_obj: Model,
    ) -> Response | StreamingResponse:
        """Forward authenticated request to upstream service with cost tracking."""
        try:
            response = await self.http_forwarder.forward_request(
                request=request,
                path=path,
                headers=headers,
                request_body=request_body,
                query_params=self.prepare_params(path, request.query_params),
                transform_body_func=self.prepare_request_body,
                model_obj=model_obj,
            )

            if response.status_code != 200:
                try:
                    mapped_error = await self.map_upstream_error_response(
                        request, path, response
                    )
                finally:
                    await response.aclose()
                return mapped_error

            if path.endswith("chat/completions"):
                client_wants_streaming = False
                if request_body:
                    try:
                        request_data = json.loads(request_body)
                        client_wants_streaming = request_data.get("stream", False)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Failed to parse request body JSON for streaming detection"
                        )

                content_type = response.headers.get("content-type", "")
                upstream_is_streaming = "text/event-stream" in content_type
                is_streaming = client_wants_streaming and upstream_is_streaming

                if is_streaming and response.status_code == 200:
                    return await self.handle_streaming_chat_completion(
                        response, key, max_cost_for_model
                    )
                elif response.status_code == 200:
                    try:
                        return await self.handle_non_streaming_chat_completion(
                            response, key, session, max_cost_for_model
                        )
                    finally:
                        await response.aclose()

            background_tasks = BackgroundTasks()
            background_tasks.add_task(response.aclose)

            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                headers=dict(response.headers),
                background=background_tasks,
            )

        except httpx.RequestError as exc:
            return create_error_response(
                "upstream_error", str(exc), 502, request=request
            )
        except Exception as exc:
            logger.error(
                "Unexpected error in upstream forwarding",
                extra={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return create_error_response(
                "internal_error",
                "An unexpected server error occurred",
                500,
                request=request,
            )

    async def forward_responses_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        request_body: bytes | None,
        key: ApiKey,
        max_cost_for_model: int,
        session: AsyncSession,
        model_obj: Model,
    ) -> Response | StreamingResponse:
        """Forward authenticated Responses API request to upstream service with cost tracking."""
        try:
            response = await self.http_forwarder.forward_request(
                request=request,
                path=path,
                headers=headers,
                request_body=request_body,
                query_params=self.prepare_params(path, request.query_params),
                transform_body_func=self.prepare_responses_request_body,
                model_obj=model_obj,
            )

            if response.status_code != 200:
                try:
                    mapped_error = await self.map_upstream_error_response(
                        request, path, response
                    )
                finally:
                    await response.aclose()
                return mapped_error

            if path.startswith("responses"):
                content_type = response.headers.get("content-type", "")
                is_streaming = "text/event-stream" in content_type

                if is_streaming and response.status_code == 200:
                    return await self.handle_streaming_responses_completion(
                        response, key, max_cost_for_model
                    )

                if response.status_code == 200:
                    try:
                        return await self.handle_non_streaming_responses_completion(
                            response, key, session, max_cost_for_model
                        )
                    finally:
                        await response.aclose()

            background_tasks = BackgroundTasks()
            background_tasks.add_task(response.aclose)

            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                headers=dict(response.headers),
                background=background_tasks,
            )

        except httpx.RequestError as exc:
            return create_error_response(
                "upstream_error", str(exc), 502, request=request
            )
        except Exception as exc:
            logger.error(
                "Unexpected error in upstream Responses API forwarding",
                extra={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
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
    ) -> Response | StreamingResponse:
        """Forward unauthenticated GET request to upstream service."""
        try:
            response = await self.http_forwarder.forward_request(
                request=request,
                path=path,
                headers=headers,
                request_body=None,
                query_params=self.prepare_params(path, request.query_params),
            )

            if response.status_code != 200:
                try:
                    mapped = await self.map_upstream_error_response(
                        request, path, response
                    )
                finally:
                    await response.aclose()
                return mapped

            background_tasks = BackgroundTasks()
            background_tasks.add_task(response.aclose)

            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                headers=dict(response.headers),
                background=background_tasks,
            )

        except Exception as exc:
            logger.error(
                "Error forwarding GET request",
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
            return create_error_response(
                "internal_error",
                "An unexpected server error occurred",
                500,
                request=request,
            )

    async def get_x_cashu_cost(
        self, response_data: dict, max_cost_for_model: int
    ) -> MaxCostData | CostData | None:
        """Calculate cost for X-Cashu payment based on response data."""
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

    # X-Cashu handlers now use modular components
    async def handle_x_cashu(
        self,
        request: Request,
        x_cashu_token: str,
        path: str,
        max_cost_for_model: int,
        model_obj: Model,
    ) -> Response | StreamingResponse:
        """Handle request with X-Cashu token payment for chat completions."""
        return await self.chat_cashu_handler.handle_request(
            request=request,
            x_cashu_token=x_cashu_token,
            path=path,
            headers=self.prepare_headers(dict(request.headers)),
            max_cost_for_model=max_cost_for_model,
            model_obj=model_obj,
            prepare_request_body_func=self.prepare_request_body,
            get_x_cashu_cost_func=self.get_x_cashu_cost,
            query_params_func=self.prepare_params,
        )

    async def handle_x_cashu_responses(
        self,
        request: Request,
        x_cashu_token: str,
        path: str,
        max_cost_for_model: int,
        model_obj: Model,
    ) -> Response | StreamingResponse:
        """Handle request with X-Cashu token payment for Responses API."""
        return await self.responses_cashu_handler.handle_request(
            request=request,
            x_cashu_token=x_cashu_token,
            path=path,
            headers=self.prepare_headers(dict(request.headers)),
            max_cost_for_model=max_cost_for_model,
            model_obj=model_obj,
            prepare_responses_request_body_func=self.prepare_responses_request_body,
            get_x_cashu_cost_func=self.get_x_cashu_cost,
            query_params_func=self.prepare_params,
        )

    def _apply_provider_fee_to_model(self, model: Model) -> Model:
        """Apply provider fee to model's USD pricing and calculate max costs."""
        adjusted_pricing = Pricing.parse_obj(
            {k: v * self.provider_fee for k, v in model.pricing.dict().items()}
        )

        temp_model = Model(
            id=model.id,
            name=model.name,
            created=model.created,
            description=model.description,
            context_length=model.context_length,
            architecture=model.architecture,
            pricing=adjusted_pricing,
            sats_pricing=None,
            per_request_limits=model.per_request_limits,
            top_provider=model.top_provider,
            enabled=model.enabled,
            upstream_provider_id=model.upstream_provider_id,
            canonical_slug=model.canonical_slug,
            alias_ids=model.alias_ids,
        )

        (
            adjusted_pricing.max_prompt_cost,
            adjusted_pricing.max_completion_cost,
            adjusted_pricing.max_cost,
        ) = _calculate_usd_max_costs(temp_model)

        return Model(
            id=model.id,
            name=model.name,
            created=model.created,
            description=model.description,
            context_length=model.context_length,
            architecture=model.architecture,
            pricing=adjusted_pricing,
            sats_pricing=model.sats_pricing,
            per_request_limits=model.per_request_limits,
            top_provider=model.top_provider,
            enabled=model.enabled,
            upstream_provider_id=model.upstream_provider_id,
            canonical_slug=model.canonical_slug,
            alias_ids=model.alias_ids,
        )

    async def fetch_models(self) -> list[Model]:
        """Fetch available models from upstream API and update cache."""
        try:
            or_models, provider_models_response = await asyncio.gather(
                self._fetch_openrouter_models(),
                self._fetch_provider_models(),
            )

            provider_model_ids = self._parse_model_ids(provider_models_response)

            found_models = []
            not_found_models = []

            for model_id in provider_model_ids:
                or_model = self._match_model(model_id, or_models)
                if or_model:
                    try:
                        model = Model(**or_model)  # type: ignore
                        found_models.append(model)
                    except Exception as e:
                        logger.warning(
                            f"Failed to parse model {model_id}",
                            extra={"error": str(e), "error_type": type(e).__name__},
                        )
                else:
                    not_found_models.append(model_id)

            if not_found_models:
                logger.debug(
                    f"({len(not_found_models)}/{len(provider_model_ids)}) unmatched models for {self.provider_type or self.base_url}",
                    extra={"not_found_models": not_found_models},
                )

            return found_models

        except Exception as e:
            logger.error(
                f"Error fetching models for {self.provider_type or self.base_url}",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            return []

    async def _fetch_openrouter_models(self) -> list[dict]:
        """Fetch models from OpenRouter API."""
        url = "https://openrouter.ai/api/v1/models"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            models = response.json()
            return [
                model
                for model in models.get("data", [])
                if ":free" not in model.get("id", "").lower()
            ]

    async def _fetch_provider_models(self) -> dict:
        """Fetch models from provider's API."""
        url = f"{self.base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()

    def _parse_model_ids(self, response: dict) -> list[str]:
        """Parse model IDs from provider response."""
        return [model.get("id") for model in response.get("data", []) if "id" in model]

    def _match_model(self, model_id: str, or_models: list[dict]) -> dict | None:
        """Match provider model ID with OpenRouter model."""
        return next(
            (
                model
                for model in or_models
                if (model.get("id") == model_id)
                or (model.get("id", "").split("/")[-1] == model_id)
                or (model.get("canonical_slug") == model_id)
                or (model.get("canonical_slug", "").split("/")[-1] == model_id)
            ),
            None,
        )

    async def refresh_models_cache(self) -> None:
        """Refresh the in-memory models cache from upstream API."""
        try:
            models = await self.fetch_models()
            models_with_fees = [self._apply_provider_fee_to_model(m) for m in models]

            try:
                sats_to_usd = sats_usd_price()
                self._models_cache = [
                    _update_model_sats_pricing(m, sats_to_usd) for m in models_with_fees
                ]
            except Exception:
                self._models_cache = models_with_fees

            self._models_by_id = {m.id: m for m in self._models_cache}

        except Exception as e:
            logger.error(
                f"Failed to refresh models cache for {self.provider_type or self.base_url}",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

    def get_cached_models(self) -> list[Model]:
        """Get cached models for this provider."""
        return self._models_cache

    def get_cached_model_by_id(self, model_id: str) -> Model | None:
        """Get a specific cached model by ID."""
        return self._models_by_id.get(model_id)

    @classmethod
    async def create_account_static(cls) -> dict[str, object]:
        """Create a new account with the provider (class method, no instance needed)."""
        raise NotImplementedError(
            f"Provider {cls.provider_type} does not support account creation"
        )

    async def create_account(self) -> dict[str, object]:
        """Create a new account with the provider."""
        raise NotImplementedError(
            f"Provider {self.provider_type} does not support account creation"
        )

    async def initiate_topup(self, amount: int) -> TopupData:
        """Initiate a Lightning Network top-up for the provider account."""
        raise NotImplementedError(
            f"Provider {self.provider_type} does not support top-up"
        )

    async def get_balance(self) -> float | None:
        """Get the current account balance from the provider."""
        raise NotImplementedError(
            f"Provider {self.provider_type} does not support balance checking"
        )