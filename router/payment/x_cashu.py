import json
import traceback
from typing import AsyncGenerator, Literal, cast

import httpx
from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from router.cashu import wallet
from router.payment.cost_caculation import (
    CostData,
    CostDataError,
    MaxCostData,
    calculate_cost,
)
from router.payment.helpers import (
    UPSTREAM_BASE_URL,
    create_error_response,
    get_max_cost_for_model,
    prepare_upstream_headers,
)


async def x_cashu_handler(
    request: Request, x_cashu_token: str, path: str
) -> Response | StreamingResponse:
    headers = dict(request.headers)
    amount, _ = await redeem_token(x_cashu_token)
    headers = prepare_upstream_headers(dict(request.headers))
    return await forward_to_upstream(request, path, headers, amount)


async def forward_to_upstream(
    request: Request, path: str, headers: dict, amount: int
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
                stream=True,
            )

            if path.endswith("chat/completions"):
                result = await handle_x_cashu_chat_completion(response, amount)
                background_tasks = BackgroundTasks()
                background_tasks.add_task(response.aclose)
                result.background = background_tasks
                return result

            background_tasks = BackgroundTasks()
            background_tasks.add_task(response.aclose)
            background_tasks.add_task(client.aclose)

            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                headers=dict(response.headers),
                background=background_tasks,
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


async def handle_x_cashu_chat_completion(
    response: httpx.Response, amount: int
) -> StreamingResponse | Response:
    """Handle both streaming and non-streaming chat completion responses with token-based pricing."""
    try:
        content = await response.aread()
        content_str = content.decode("utf-8") if isinstance(content, bytes) else content
        is_streaming = content_str.startswith("data:") or "data:" in content_str

        if is_streaming:
            print("Detected streaming response, processing SSE format")
            return await handle_streaming_response(content_str, response, amount)
        else:
            print("Detected non-streaming response, processing as JSON")
            return await handle_non_streaming_response(content_str, response, amount)

    except Exception as e:
        print(f"Error processing chat completion response: {e}")
        # Return the original response if we can't process it
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=dict(response.headers),
        )


async def handle_streaming_response(
    content_str: str, response: httpx.Response, amount: int
) -> StreamingResponse:
    """Handle Server-Sent Events (SSE) streaming response."""
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

    print(f"usage: {usage_data}")
    # If we found usage data, calculate cost and refund
    if usage_data and model:
        response_data = {"usage": usage_data, "model": model}
        try:
            cost_data = await get_cost(response_data)
            print(f"Refunded {cost_data} msats")
            if cost_data:
                refund_amount = amount - cost_data.total_msats
                if refund_amount > 0:
                    refund_token = await send_refund(refund_amount)
                    response.headers["X-Cashu"] = refund_token
                    print(f"Refunded {refund_amount} msats")
        except Exception as e:
            print(f"Error calculating cost for streaming response: {e}")

    response_headers = dict(response.headers)
    if "transfer-encoding" in response_headers:
        del response_headers["transfer-encoding"]
    if "content-encoding" in response_headers:
        del response_headers["content-encoding"]

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
    content_str: str, response: httpx.Response, amount: int
) -> Response:
    """Handle regular JSON response."""
    try:
        response_json = json.loads(content_str)

        cost_data = await get_cost(response_json)

        if not cost_data:
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

        refund_amount = amount - cost_data.total_msats
        print("refund: ", refund_amount)
        if refund_amount > 0:
            refund_token = await send_refund(refund_amount)
            response.headers["X-Cashu"] = refund_token
            print(f"Refunded {refund_amount} msats")

        return Response(
            content=content_str,
            status_code=response.status_code,
            headers=response_headers,
            media_type="application/json",
        )
    except json.JSONDecodeError as e:
        response.headers["X-Cashu"] = await wallet().send(amount - 60)
        print(f"Failed to parse JSON from upstream response: {e}")
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
    max_cost = get_max_cost_for_model(model=response_data["model"])

    match calculate_cost(response_data, max_cost):
        case MaxCostData() as cost:
            return cost
        case CostData() as cost:
            return cost
        case CostDataError() as error:
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


async def redeem_token(x_cashu_token: str) -> tuple[int, Literal["sat", "msat"]]:
    try:
        result = await wallet().redeem(x_cashu_token)
        return cast(tuple[int, Literal["sat", "msat"]], result)
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": f"Invalid or expired Cashu key: {str(e)}",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )


async def send_refund(amount: int) -> str:
    try:
        return await wallet().send(amount)
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": f"failed to create refund: {str(e)}",
                    "type": "invalid_request_error",
                    "code": "send_token_failed",
                }
            },
        )
