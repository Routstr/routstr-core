import json
import traceback
from typing import AsyncGenerator

import httpx
from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ..core import get_logger
from ..wallet import CurrencyUnit, recieve_token, send_token
from .cost_caculation import CostData, CostDataError, MaxCostData, calculate_cost
from .helpers import (
    UPSTREAM_BASE_URL,
    create_error_response,
    get_max_cost_for_model,
    get_upstream_url_for_request,
    prepare_upstream_headers,
)

logger = get_logger(__name__)


async def x_cashu_handler(
    request: Request, x_cashu_token: str, path: str
) -> Response | StreamingResponse:
    """Handle X-Cashu token payment requests."""
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
        # Extract model name for proper header preparation
        model_name = None
        if request_body_dict:
            from .helpers import extract_model_from_request

            model_name = extract_model_from_request(request_body_dict)
        headers = prepare_upstream_headers(dict(request.headers), model_name)

        logger.info(
            "X-Cashu token redeemed successfully",
            extra={"amount": amount, "unit": unit, "path": path, "mint": mint},
        )

        # Parse request body to extract model name for provider URL lookup
        request_body_dict = None
        try:
            request_body = await request.body()
            if request_body:
                request_body_dict = json.loads(request_body)
        except (json.JSONDecodeError, Exception):
            pass  # Continue with default URL if body parsing fails

        return await forward_to_upstream(
            request, path, headers, amount, unit, request_body_dict
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

        # Handle specific CASHU errors with appropriate HTTP status codes
        if "already spent" in error_message.lower():
            return create_error_response(
                "token_already_spent",
                "The provided CASHU token has already been spent",
                400,
                x_cashu_token,
            )

        if "invalid token" in error_message.lower():
            return create_error_response(
                "invalid_token",
                "The provided CASHU token is invalid",
                400,
                x_cashu_token,
            )

        if "mint error" in error_message.lower():
            return create_error_response(
                "mint_error", f"CASHU mint error: {error_message}", 422, x_cashu_token
            )

        # Generic error for other cases
        return create_error_response(
            "cashu_error",
            f"CASHU token processing failed: {error_message}",
            400,
            x_cashu_token,
        )


async def forward_to_upstream(
    request: Request,
    path: str,
    headers: dict,
    amount: int,
    unit: CurrencyUnit,
    request_body_dict: dict | None = None,
) -> Response | StreamingResponse:
    """Forward request to upstream and handle the response."""
    url = get_upstream_url_for_request(path, request_body_dict)

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
                    params=request.query_params,
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

                result = await handle_x_cashu_chat_completion(response, amount, unit)
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
                "internal_error", "An unexpected server error occurred", 500
            )


async def handle_x_cashu_chat_completion(
    response: httpx.Response, amount: int, unit: CurrencyUnit
) -> StreamingResponse | Response:
    """Handle both streaming and non-streaming chat completion responses with token-based pricing."""
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
            return await handle_streaming_response(content_str, response, amount, unit)
        else:
            return await handle_non_streaming_response(
                content_str, response, amount, unit
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
        # Return the original response if we can't process it
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=dict(response.headers),
        )


async def handle_streaming_response(
    content_str: str, response: httpx.Response, amount: int, unit: CurrencyUnit
) -> StreamingResponse:
    """Handle Server-Sent Events (SSE) streaming response."""
    logger.debug(
        "Processing streaming response",
        extra={
            "amount": amount,
            "unit": unit,
            "content_lines": len(content_str.strip().split("\n")),
        },
    )

    # Initialize response headers early so they can be modified during processing
    response_headers = dict(response.headers)
    if "transfer-encoding" in response_headers:
        del response_headers["transfer-encoding"]
    if "content-encoding" in response_headers:
        del response_headers["content-encoding"]

    # For streaming responses, we'll extract the final usage data
    # and calculate cost based on that
    usage_data = None
    model = None

    # Parse SSE format to extract usage information
    lines = content_str.strip().split("\n")
    for line in lines:
        if line.startswith("data: "):
            try:
                data_json = json.loads(line[6:])  # Remove 'data: ' prefix
                # Look for usage information in the final chunks
                if "usage" in data_json:
                    usage_data = data_json["usage"]
                    model = data_json.get("model")
                elif "model" in data_json and not model:
                    model = data_json["model"]
            except json.JSONDecodeError:
                continue

    response_headers = dict(response.headers)
    # If we found usage data, calculate cost and refund
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
            cost_data = await get_cost(response_data)
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


async def handle_non_streaming_response(
    content_str: str, response: httpx.Response, amount: int, unit: CurrencyUnit
) -> Response:
    """Handle regular JSON response."""
    logger.debug(
        "Processing non-streaming response",
        extra={"amount": amount, "unit": unit, "content_length": len(content_str)},
    )

    try:
        response_json = json.loads(content_str)

        cost_data = await get_cost(response_json)

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

        # Emergency refund with small deduction for processing
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

        # Return original content if JSON parsing fails
        return Response(
            content=content_str,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type="application/json",
        )


async def get_cost(response_data: dict) -> MaxCostData | CostData | None:
    """
    Adjusts the payment based on token usage in the response.
    This is called after the initial payment and the upstream request is complete.
    Returns cost data to be included in the response.
    """
    model = response_data.get("model", "unknown")
    logger.debug(
        "Calculating cost for response",
        extra={"model": model, "has_usage": "usage" in response_data},
    )

    max_cost = get_max_cost_for_model(model=model)

    match calculate_cost(response_data, max_cost):
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


async def send_refund(amount: int, unit: CurrencyUnit, mint: str | None = None) -> str:
    """Send a refund using Cashu tokens."""
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

    # If we get here, all retries failed
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
