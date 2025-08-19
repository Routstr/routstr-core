import json
import re
import traceback
from typing import AsyncGenerator

import httpx
from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ..core import get_logger
from ..wallet import recieve_token, send_token
from .cost_caculation import CostData, CostDataError, MaxCostData, calculate_cost
from .helpers import (
    UPSTREAM_BASE_URL,
    create_error_response,
    get_max_cost_for_model,
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
        headers = prepare_upstream_headers(dict(request.headers))

        logger.info(
            "X-Cashu token redeemed successfully",
            extra={"amount": amount, "unit": unit, "path": path, "mint": mint},
        )

        return await forward_to_upstream(request, path, headers, amount, unit)
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


async def forward_to_upstream(
    request: Request, path: str, headers: dict, amount: int, unit: str
) -> Response | StreamingResponse:
    """Forward request to upstream and handle the response."""
    if path.startswith("v1/"):
        path = path.replace("v1/", "")

    url = f"{UPSTREAM_BASE_URL}/{path}"

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

    client = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=1),
        timeout=None,
    )

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

            await response.aclose()
            await client.aclose()

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

            content_type = response.headers.get("content-type", "")
            is_streaming = "text/event-stream" in content_type

            if is_streaming:
                result = await handle_streaming_response(
                    request, response, amount, unit
                )
                background_tasks = BackgroundTasks()
                background_tasks.add_task(response.aclose)
                background_tasks.add_task(client.aclose)
                result.background = background_tasks
                return result
            else:
                try:
                    return await handle_non_streaming_response(response, amount, unit)
                finally:
                    await response.aclose()
                    await client.aclose()

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
                "traceback": tb,
            },
        )
        return create_error_response(
            "internal_error",
            "An unexpected server error occurred",
            500,
            request=request,
        )


async def handle_streaming_response(
    request: Request, response: httpx.Response, amount: int, unit: str
) -> StreamingResponse:
    """Handle Server-Sent Events (SSE) streaming response."""
    logger.debug(
        "Processing streaming response",
        extra={
            "amount": amount,
            "unit": unit,
            "status_code": response.status_code,
        },
    )

    model = "unknown"
    try:
        request_body = await request.body()
        if request_body:
            request_data = json.loads(request_body)
            model = request_data.get("model", "unknown")
    except Exception as e:
        logger.warning(
            "Could not extract model from request for streaming response",
            extra={"error": str(e), "amount": amount, "unit": unit},
        )

    # For streaming responses, we can't calculate exact cost upfront
    # Instead, we'll hold most of the payment and refund properly after streaming
    # Only refund a small conservative amount upfront to show the user something is happening
    upfront_refund_percentage = 0.1  # Only refund 10% upfront
    upfront_refund_amount = int(amount * upfront_refund_percentage)

    # Ensure minimum refund is reasonable but not too much
    min_upfront = 100  # msat
    max_upfront = 5000  # msat

    if unit == "sat":
        min_upfront = 1  # sat
        max_upfront = 50  # sat

    refund_amount = max(min_upfront, min(upfront_refund_amount, max_upfront))

    refund_token = None
    if refund_amount > 0:
        try:
            refund_token = await send_refund(refund_amount, unit)
            logger.info(
                "Conservative upfront refund issued for streaming response",
                extra={
                    "original_amount": amount,
                    "upfront_refund": refund_amount,
                    "upfront_percentage": f"{upfront_refund_percentage * 100}%",
                    "unit": unit,
                    "model": model,
                    "note": "Small upfront refund - actual refund calculated after streaming",
                },
            )
        except Exception as refund_error:
            logger.error(
                "Failed to issue estimated refund for streaming response",
                extra={
                    "error": str(refund_error),
                    "amount": amount,
                    "unit": unit,
                },
            )

    response_headers = dict(response.headers)
    if "transfer-encoding" in response_headers:
        del response_headers["transfer-encoding"]
    if "content-encoding" in response_headers:
        del response_headers["content-encoding"]

    if refund_token:
        response_headers["X-Cashu"] = refund_token

    async def stream_with_monitoring() -> AsyncGenerator[bytes, None]:
        """Stream response while monitoring usage for logging."""
        stored_chunks = []

        async for chunk in response.aiter_bytes():
            stored_chunks.append(chunk)
            yield chunk

        logger.debug(
            "Streaming completed, analyzing usage for monitoring",
            extra={
                "amount": amount,
                "unit": unit,
                "chunks_count": len(stored_chunks),
                "estimated_refund_issued": refund_amount,
            },
        )

        usage_data = None
        response_model = None

        for i in range(len(stored_chunks) - 1, -1, -1):
            chunk = stored_chunks[i]
            if not chunk or chunk == b"":
                continue

            try:
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
                            usage_data = data["usage"]
                            response_model = data.get("model", model)
                            break
                        elif "model" in data and not response_model:
                            response_model = data["model"]
                    except json.JSONDecodeError:
                        continue

                if usage_data:
                    break

            except Exception as e:
                logger.error(
                    "Error processing streaming response chunk for monitoring",
                    extra={
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "amount": amount,
                        "unit": unit,
                    },
                )

        if usage_data and response_model:
            try:
                cost_data = await get_cost(
                    {"usage": usage_data, "model": response_model}
                )
                print(cost_data)
                if cost_data:
                    if unit == "msat":
                        actual_cost = cost_data.total_msats
                    elif unit == "sat":
                        actual_cost = (cost_data.total_msats + 999) // 1000
                    else:
                        actual_cost = 0

                    ideal_total_refund = amount - actual_cost
                    additional_refund_needed = ideal_total_refund - refund_amount

                    if additional_refund_needed > 0:
                        try:
                            additional_refund_token = await send_refund(
                                additional_refund_needed, unit
                            )
                            logger.info(
                                "Additional refund issued after streaming analysis",
                                extra={
                                    "original_amount": amount,
                                    "actual_cost": actual_cost,
                                    "upfront_refund": refund_amount,
                                    "additional_refund": additional_refund_needed,
                                    "total_refund": ideal_total_refund,
                                    "unit": unit,
                                    "model": response_model,
                                    "additional_refund_token": additional_refund_token[
                                        :20
                                    ]
                                    + "..."
                                    if len(additional_refund_token) > 20
                                    else additional_refund_token,
                                },
                            )
                        except Exception as refund_error:
                            logger.error(
                                "Failed to issue additional refund after streaming",
                                extra={
                                    "error": str(refund_error),
                                    "additional_refund_needed": additional_refund_needed,
                                    "unit": unit,
                                    "model": response_model,
                                },
                            )
                    else:
                        logger.info(
                            "No additional refund needed after streaming analysis",
                            extra={
                                "original_amount": amount,
                                "actual_cost": actual_cost,
                                "upfront_refund": refund_amount,
                                "ideal_total_refund": ideal_total_refund,
                                "unit": unit,
                                "model": response_model,
                                "note": "Upfront refund was sufficient or overpaid",
                            },
                        )
                else:
                    logger.warning(
                        "Could not calculate actual cost for streaming response",
                        extra={"amount": amount, "unit": unit, "model": response_model},
                    )
            except Exception as e:
                logger.error(
                    "Error analyzing actual usage for streaming response",
                    extra={
                        "error": str(e),
                        "amount": amount,
                        "unit": unit,
                        "model": response_model,
                    },
                )
        else:
            # No usage data found, issue emergency refund
            emergency_refund = (
                amount - refund_amount - 60
            )  # Total minus upfront minus processing fee
            if emergency_refund > 0:
                try:
                    emergency_refund_token = await send_refund(emergency_refund, unit)
                    logger.warning(
                        "Emergency refund issued - no usage data found",
                        extra={
                            "amount": amount,
                            "upfront_refund": refund_amount,
                            "emergency_refund": emergency_refund,
                            "total_refund": refund_amount + emergency_refund,
                            "unit": unit,
                            "estimated_model": model,
                            "emergency_token": emergency_refund_token[:20] + "..."
                            if len(emergency_refund_token) > 20
                            else emergency_refund_token,
                        },
                    )
                except Exception as refund_error:
                    logger.error(
                        "Failed to issue emergency refund for no usage data",
                        extra={
                            "error": str(refund_error),
                            "emergency_refund": emergency_refund,
                            "unit": unit,
                        },
                    )
            else:
                logger.warning(
                    "No usage data found in streaming response",
                    extra={
                        "amount": amount,
                        "unit": unit,
                        "estimated_model": model,
                        "upfront_refund": refund_amount,
                    },
                )

    return StreamingResponse(
        stream_with_monitoring(),
        status_code=response.status_code,
        headers=response_headers,
    )


async def handle_non_streaming_response(
    response: httpx.Response, amount: int, unit: str
) -> Response:
    """Handle regular JSON response."""
    logger.debug(
        "Processing non-streaming response",
        extra={"amount": amount, "unit": unit, "status_code": response.status_code},
    )

    try:
        content = await response.aread()
        content_str = content.decode("utf-8") if isinstance(content, bytes) else content
        response_json = json.loads(content_str)

        cost_data = await get_cost(response_json)
        refund_token = None

        if not cost_data:
            logger.warning(
                "Failed to calculate cost for response, issuing emergency refund",
                extra={
                    "amount": amount,
                    "unit": unit,
                    "response_model": response_json.get("model", "unknown"),
                },
            )
            emergency_refund_amount = amount - 60
            if emergency_refund_amount > 0:
                try:
                    refund_token = await send_refund(emergency_refund_amount, unit)
                    logger.info(
                        "Emergency refund issued for cost calculation failure",
                        extra={
                            "refund_amount": emergency_refund_amount,
                            "unit": unit,
                            "original_amount": amount,
                        },
                    )
                except Exception as refund_error:
                    logger.error(
                        "Failed to issue emergency refund for cost calculation failure",
                        extra={
                            "error": str(refund_error),
                            "amount": amount,
                            "unit": unit,
                        },
                    )

        response_headers = dict(response.headers)
        if "transfer-encoding" in response_headers:
            del response_headers["transfer-encoding"]
        if "content-encoding" in response_headers:
            del response_headers["content-encoding"]

        if cost_data:
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

        if refund_token:
            response_headers["X-Cashu"] = refund_token

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

        emergency_refund_amount = amount - 60
        refund_token = None

        if emergency_refund_amount > 0:
            try:
                refund_token = await send_refund(emergency_refund_amount, unit)
                logger.warning(
                    "Emergency refund issued due to JSON parse error",
                    extra={
                        "original_amount": amount,
                        "refund_amount": emergency_refund_amount,
                        "deduction": 60,
                        "unit": unit,
                    },
                )
            except Exception as refund_error:
                logger.error(
                    "Failed to issue emergency refund for JSON parse error",
                    extra={
                        "error": str(refund_error),
                        "amount": amount,
                        "unit": unit,
                    },
                )

        response_headers = dict(response.headers)
        if "transfer-encoding" in response_headers:
            del response_headers["transfer-encoding"]
        if "content-encoding" in response_headers:
            del response_headers["content-encoding"]

        if refund_token:
            response_headers["X-Cashu"] = refund_token

        return Response(
            content=content_str,
            status_code=response.status_code,
            headers=response_headers,
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

    cost_result = calculate_cost(response_data, max_cost)

    if isinstance(cost_result, MaxCostData):
        logger.debug(
            "Using max cost pricing",
            extra={"model": model, "max_cost_msats": cost_result.total_msats},
        )
        return cost_result
    elif isinstance(cost_result, CostData):
        logger.debug(
            "Using token-based pricing",
            extra={
                "model": model,
                "total_cost_msats": cost_result.total_msats,
                "input_msats": cost_result.input_msats,
                "output_msats": cost_result.output_msats,
            },
        )
        return cost_result
    elif isinstance(cost_result, CostDataError):
        logger.error(
            "Cost calculation error",
            extra={
                "model": model,
                "error_message": cost_result.message,
                "error_code": cost_result.code,
            },
        )
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": cost_result.message,
                    "type": "invalid_request_error",
                    "code": cost_result.code,
                }
            },
        )


async def send_refund(amount: int, unit: str, mint: str | None = None) -> str:
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
