"""X-Cashu billing when the upstream omits usage.

The local token estimator bills from the request body and the generated text.
When nothing can be estimated the prepayment is refunded in full.
"""

import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402

REQUEST_BODY = json.dumps(
    {"model": "gpt-4o", "messages": [{"role": "user", "content": "Tell me a joke"}]}
).encode()


def _sse(events: list[dict[str, Any]]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=body.encode()
    )


def _json(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "application/json"}, content=json.dumps(payload)
    )


async def _settle(
    response: httpx.Response,
    *,
    responses_api: bool = False,
    request_body: bytes | None = REQUEST_BODY,
) -> tuple[Any, AsyncMock, AsyncMock]:
    provider = BaseUpstreamProvider(base_url="http://test", api_key="test-key")
    get_cost = AsyncMock(side_effect=provider.get_x_cashu_cost)
    send_refund = AsyncMock(return_value="cashuBrefund")
    handler = (
        provider.handle_x_cashu_responses_completion
        if responses_api
        else provider.handle_x_cashu_chat_completion
    )
    with (
        patch.object(provider, "get_x_cashu_cost", new=get_cost),
        patch.object(provider, "send_refund", new=send_refund),
    ):
        result = await handler(
            response=response,
            amount=10_000,
            unit="msat",
            max_cost_for_model=9_000,
            mint=None,
            request_body=request_body,
        )
    return result, get_cost, send_refund


def _billed_usage(get_cost: AsyncMock) -> dict[str, Any] | None:
    assert get_cost.await_args is not None
    return get_cost.await_args.args[0].get("usage")


@pytest.mark.asyncio
async def test_streaming_chat_without_usage_bills_from_estimate() -> None:
    events = [
        {"model": "gpt-4o", "choices": [{"delta": {"content": "Why did the "}}]},
        {"model": "gpt-4o", "choices": [{"delta": {"content": "chicken cross"}}]},
    ]
    _, get_cost, _ = await _settle(_sse(events))

    usage = _billed_usage(get_cost)
    assert usage is not None
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
    assert usage["estimated"] is True


@pytest.mark.asyncio
async def test_streaming_chat_without_text_refunds_everything() -> None:
    _, get_cost, send_refund = await _settle(
        _sse([{"model": "gpt-4o"}]), request_body=None
    )

    assert _billed_usage(get_cost) is None
    assert send_refund.await_args is not None
    assert send_refund.await_args.args[0] == 10_000


@pytest.mark.asyncio
async def test_non_streaming_chat_without_usage_bills_from_estimate() -> None:
    payload = {
        "model": "gpt-4o",
        "choices": [
            {"message": {"role": "assistant", "content": "To get to the other side."}}
        ],
    }
    _, get_cost, _ = await _settle(_json(payload))

    usage = _billed_usage(get_cost)
    assert usage is not None
    assert usage["output_tokens"] > 0
    assert usage["estimated"] is True


@pytest.mark.asyncio
async def test_streaming_responses_without_usage_bills_from_estimate() -> None:
    events = [
        {"type": "response.created", "response": {"model": "gpt-5-mini"}},
        {"type": "response.output_text.delta", "delta": "Why did the chicken"},
        {"type": "response.output_text.done", "text": "Why did the chicken"},
    ]
    _, get_cost, _ = await _settle(_sse(events), responses_api=True)

    usage = _billed_usage(get_cost)
    assert usage is not None
    assert usage["output_tokens"] > 0
    assert usage["estimated"] is True


@pytest.mark.asyncio
async def test_non_streaming_responses_without_usage_bills_from_estimate() -> None:
    payload = {
        "model": "gpt-5-mini",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Hi"}]}
        ],
    }
    _, get_cost, _ = await _settle(_json(payload), responses_api=True)

    usage = _billed_usage(get_cost)
    assert usage is not None
    assert usage["output_tokens"] > 0
