"""X-Cashu payment request handlers for different API types."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable

import httpx
from fastapi import BackgroundTasks, Request
from fastapi.responses import Response, StreamingResponse

from ...core import get_logger
from ...wallet import send_token
from ..payment.cashu_handler import (
    CashuTokenError,
    create_cashu_error_response,
    validate_and_redeem_token,
)
from ..processing.http_client import HttpForwarder
from ..processing.response_handler import ChatCompletionProcessor, ResponsesApiProcessor

if TYPE_CHECKING:
    from ...payment.models import Model

logger = get_logger(__name__)


class BaseCashuHandler:
    """Base handler for X-Cashu payment requests."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.http_forwarder = HttpForwarder(base_url)

    async def send_refund(self, amount: int, unit: str, mint: str | None = None) -> str:
        """Create and send a refund token to the user."""
        logger.debug(
            "Creating refund token",
            extra={"amount": amount, "unit": unit, "mint": mint},
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

        raise Exception(
            f"failed to create refund after {max_retries} attempts: {str(last_exception)}"
        )

    def _calculate_refund_amount(self, amount: int, unit: str, cost_msats: int) -> int:
        """Calculate refund amount based on unit and cost."""
        if unit == "msat":
            return amount - cost_msats
        elif unit == "sat":
            return amount - (cost_msats + 999) // 1000
        else:
            raise ValueError(f"Invalid unit: {unit}")

    async def _handle_upstream_error(
        self,
        response: httpx.Response,
        amount: int,
        unit: str,
        mint: str | None,
        api_type: str = "API",
    ) -> Response:
        """Handle upstream service errors with refund."""
        logger.warning(
            f"Upstream {api_type} request failed, processing refund",
            extra={
                "status_code": response.status_code,
                "amount": amount,
                "unit": unit,
            },
        )

        refund_token = await self.send_refund(amount - 60, unit, mint)

        logger.info(
            f"Refund processed for failed upstream {api_type} request",
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
                        "message": f"Error forwarding {api_type} request to upstream",
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


class ChatCompletionCashuHandler(BaseCashuHandler):
    """Handler for X-Cashu paid chat completion requests."""

    async def handle_request(
        self,
        request: Request,
        x_cashu_token: str,
        path: str,
        headers: dict,
        max_cost_for_model: int,
        model_obj: Model,
        prepare_request_body_func: Callable,
        get_x_cashu_cost_func: Callable,
        query_params_func: Callable,
    ) -> Response | StreamingResponse:
        """Handle chat completion request with X-Cashu payment."""
        try:
            amount, unit, mint = await validate_and_redeem_token(x_cashu_token, request)

            # Forward request to upstream
            request_body = await request.body()
            response = await self.http_forwarder.forward_request(
                request=request,
                path=path,
                headers=headers,
                request_body=request_body,
                query_params=query_params_func(path, request.query_params),
                transform_body_func=prepare_request_body_func,
                model_obj=model_obj,
            )

            # Handle upstream errors
            if response.status_code != 200:
                return await self._handle_upstream_error(
                    response, amount, unit, mint, "chat completion"
                )

            # Process chat completion response
            if path.endswith("chat/completions"):
                result = await self._handle_chat_completion_response(
                    response,
                    amount,
                    unit,
                    max_cost_for_model,
                    mint,
                    get_x_cashu_cost_func,
                )
                background_tasks = BackgroundTasks()
                background_tasks.add_task(response.aclose)
                result.background = background_tasks
                return result

            # Default streaming response for other endpoints
            return self._create_default_streaming_response(response)

        except CashuTokenError as e:
            return create_cashu_error_response(e, request, x_cashu_token)

    async def _handle_chat_completion_response(
        self,
        response: httpx.Response,
        amount: int,
        unit: str,
        max_cost_for_model: int,
        mint: str | None,
        get_x_cashu_cost_func: Callable,
    ) -> Response | StreamingResponse:
        """Handle chat completion response processing."""
        # For X-Cashu requests, we force non-streaming response even if upstream is streaming,
        # because we need to calculate the refund and include it in the headers.
        # Refund token must be in the headers, which are sent before the body.

        content = await response.aread()
        content_str = content.decode("utf-8") if isinstance(content, bytes) else content

        # Check if it was streaming content to process usage correctly
        is_streaming_content = ChatCompletionProcessor.is_streaming_response(
            content_str
        )

        if is_streaming_content:
            # Buffer streaming content to calculate refund before sending headers.
            # Headers (containing refund token) must be sent before body, preventing
            # true streaming while supporting dynamic refunds.

            # Extract usage data from buffered content
            usage_data, model = ChatCompletionProcessor.extract_usage_from_streaming(
                content_str
            )

            if usage_data and model:
                try:
                    response_data = {"usage": usage_data, "model": model}
                    cost_data = await get_x_cashu_cost_func(
                        response_data, max_cost_for_model
                    )

                    if cost_data:
                        refund_amount = self._calculate_refund_amount(
                            amount, unit, cost_data.total_msats
                        )

                        if refund_amount > 0:
                            refund_token = await self.send_refund(
                                refund_amount, unit, mint
                            )
                            # Refund token will be added to new response headers below
                except Exception as e:
                    logger.error(
                        "Error calculating refund for buffered streaming response",
                        extra={"error": str(e)},
                    )

            # We return a StreamingResponse that yields the buffered content
            # This satisfies the client expecting a stream, while allowing us to set headers.

            response_headers = ChatCompletionProcessor.clean_response_headers(
                dict(response.headers)
            )

            if "refund_token" in locals() and refund_token:
                response_headers["X-Cashu"] = refund_token

            lines = content_str.strip().split("\n")
            return StreamingResponse(
                ChatCompletionProcessor.create_streaming_generator(lines),
                status_code=response.status_code,
                headers=response_headers,
                media_type="text/event-stream",  # Keep original media type if possible, or text/event-stream
            )
        else:
            return await self._handle_non_streaming_response(
                content_str,
                response,
                amount,
                unit,
                max_cost_for_model,
                mint,
                get_x_cashu_cost_func,
            )

    async def _handle_non_streaming_response(
        self,
        content_str: str,
        response: httpx.Response,
        amount: int,
        unit: str,
        max_cost_for_model: int,
        mint: str | None,
        get_x_cashu_cost_func: Callable,
    ) -> Response:
        """Handle non-streaming chat completion response with refund calculation."""
        response_headers = ChatCompletionProcessor.clean_response_headers(
            dict(response.headers)
        )

        try:
            response_json = json.loads(content_str)
            cost_data = await get_x_cashu_cost_func(response_json, max_cost_for_model)

            if cost_data:
                refund_amount = self._calculate_refund_amount(
                    amount, unit, cost_data.total_msats
                )

                if refund_amount > 0:
                    refund_token = await self.send_refund(refund_amount, unit, mint)
                    response_headers["X-Cashu"] = refund_token

            return Response(
                content=content_str,
                status_code=response.status_code,
                headers=response_headers,
                media_type="application/json",
            )

        except json.JSONDecodeError:
            # Emergency refund on parse error
            emergency_refund = amount
            refund_token = await send_token(emergency_refund, unit=unit, mint_url=mint)
            response_headers["X-Cashu"] = refund_token

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
                headers=response_headers,
                media_type="application/json",
            )

    def _create_default_streaming_response(
        self, response: httpx.Response
    ) -> StreamingResponse:
        """Create default streaming response for non-chat endpoints."""
        background_tasks = BackgroundTasks()
        background_tasks.add_task(response.aclose)

        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=dict(response.headers),
            background=background_tasks,
        )


class ResponsesApiCashuHandler(BaseCashuHandler):
    """Handler for X-Cashu paid Responses API requests."""

    async def handle_request(
        self,
        request: Request,
        x_cashu_token: str,
        path: str,
        headers: dict,
        max_cost_for_model: int,
        model_obj: Model,
        prepare_responses_api_request_body_func: Callable,
        get_x_cashu_cost_func: Callable,
        query_params_func: Callable,
    ) -> Response | StreamingResponse:
        """Handle Responses API request with X-Cashu payment."""
        try:
            amount, unit, mint = await validate_and_redeem_token(x_cashu_token, request)

            # Forward request to upstream
            request_body = await request.body()
            response = await self.http_forwarder.forward_request(
                request=request,
                path=path,
                headers=headers,
                request_body=request_body,
                query_params=query_params_func(path, request.query_params),
                transform_body_func=prepare_responses_api_request_body_func,
                model_obj=model_obj,
            )

            # Handle upstream errors
            if response.status_code != 200:
                return await self._handle_upstream_error(
                    response, amount, unit, mint, "Responses API"
                )

            # Process Responses API response
            if path.startswith("responses"):
                result = await self._handle_responses_api_completion(
                    response,
                    amount,
                    unit,
                    max_cost_for_model,
                    mint,
                    get_x_cashu_cost_func,
                )
                background_tasks = BackgroundTasks()
                background_tasks.add_task(response.aclose)
                result.background = background_tasks
                return result

            # Default streaming response for other endpoints
            return self._create_default_streaming_response(response)

        except CashuTokenError as e:
            return create_cashu_error_response(e, request, x_cashu_token)

    async def _handle_responses_api_completion(
        self,
        response: httpx.Response,
        amount: int,
        unit: str,
        max_cost_for_model: int,
        mint: str | None,
        get_x_cashu_cost_func: Callable,
    ) -> Response | StreamingResponse:
        """Handle Responses API completion response processing."""
        # Force non-streaming for X-Cashu to handle refunds in headers
        content = await response.aread()
        content_str = content.decode("utf-8") if isinstance(content, bytes) else content

        is_streaming_content = ResponsesApiProcessor.is_streaming_response(content_str)

        if is_streaming_content:
            usage_data, model = (
                ResponsesApiProcessor.extract_usage_with_reasoning_tokens(content_str)
            )

            refund_token = None
            if usage_data and model:
                try:
                    response_data = {"usage": usage_data, "model": model}
                    cost_data = await get_x_cashu_cost_func(
                        response_data, max_cost_for_model
                    )

                    if cost_data:
                        refund_amount = self._calculate_refund_amount(
                            amount, unit, cost_data.total_msats
                        )

                        if refund_amount > 0:
                            refund_token = await self.send_refund(
                                refund_amount, unit, mint
                            )
                except Exception as e:
                    logger.error(
                        "Error calculating refund for buffered Responses API response",
                        extra={"error": str(e)},
                    )

            response_headers = ResponsesApiProcessor.clean_response_headers(
                dict(response.headers)
            )

            if refund_token:
                response_headers["X-Cashu"] = refund_token

            lines = content_str.strip().split("\n")
            return StreamingResponse(
                ResponsesApiProcessor.create_streaming_generator(lines),
                status_code=response.status_code,
                headers=response_headers,
                media_type="text/event-stream",
            )
        else:
            return await self._handle_non_streaming_responses_api_response(
                content_str,
                response,
                amount,
                unit,
                max_cost_for_model,
                mint,
                get_x_cashu_cost_func,
            )

    async def _handle_non_streaming_responses_api_response(
        self,
        content_str: str,
        response: httpx.Response,
        amount: int,
        unit: str,
        max_cost_for_model: int,
        mint: str | None,
        get_x_cashu_cost_func: Callable,
    ) -> Response:
        """Handle non-streaming Responses API response with refund calculation."""
        response_headers = ResponsesApiProcessor.clean_response_headers(
            dict(response.headers)
        )

        try:
            response_json = json.loads(content_str)
            cost_data = await get_x_cashu_cost_func(response_json, max_cost_for_model)

            if cost_data:
                refund_amount = self._calculate_refund_amount(
                    amount, unit, cost_data.total_msats
                )

                if refund_amount > 0:
                    refund_token = await self.send_refund(refund_amount, unit, mint)
                    response_headers["X-Cashu"] = refund_token

            return Response(
                content=content_str,
                status_code=response.status_code,
                headers=response_headers,
                media_type="application/json",
            )

        except json.JSONDecodeError:
            # Emergency refund on parse error
            emergency_refund = amount
            refund_token = await self.send_refund(
                emergency_refund, unit=unit, mint=mint
            )
            response_headers["X-Cashu"] = refund_token

            logger.warning(
                "Emergency refund issued due to JSON parse error in Responses API",
                extra={
                    "original_amount": amount,
                    "refund_amount": emergency_refund,
                    "deduction": 60,
                },
            )

            return Response(
                content=content_str,
                status_code=response.status_code,
                headers=response_headers,
                media_type="application/json",
            )

    def _create_default_streaming_response(
        self, response: httpx.Response
    ) -> StreamingResponse:
        """Create default streaming response for non-chat endpoints."""
        background_tasks = BackgroundTasks()
        background_tasks.add_task(response.aclose)

        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=dict(response.headers),
            background=background_tasks,
        )
