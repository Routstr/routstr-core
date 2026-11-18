"""X-Cashu billing when the upstream omits usage.

The local token estimator bills from the request body and the generated text.
When nothing can be estimated the prepayment is refunded in full.
"""

import json
import logging
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
    unit: str = "msat",
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
            unit=unit,
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
    events: list[dict[str, Any]] = [
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


@pytest.mark.asyncio
@pytest.mark.parametrize("responses_api", [False, True])
async def test_empty_streaming_usage_uses_estimate(responses_api: bool) -> None:
    payload: dict[str, Any] = {"model": "gpt-4o", "usage": {}}
    if responses_api:
        payload["output"] = [{"content": [{"type": "output_text", "text": "Hello"}]}]
    else:
        payload["choices"] = [{"delta": {"content": "Hello"}}]

    _, get_cost, _ = await _settle(_sse([payload]), responses_api=responses_api)

    usage = _billed_usage(get_cost)
    assert usage is not None
    assert usage.get("estimated") is True
    assert usage["output_tokens"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_type", ["response.completed", "response.incomplete"])
async def test_responses_terminal_output_is_not_billed_twice(
    terminal_type: str,
) -> None:
    payload = {
        "model": "gpt-4o",
        "output": [{"content": [{"type": "output_text", "text": "Hello world"}]}],
    }
    events: list[dict[str, Any]] = [
        {"type": "response.output_text.delta", "delta": "Hello world"},
        {"type": terminal_type, "response": payload},
    ]
    _, streaming_cost, _ = await _settle(_sse(events), responses_api=True)
    _, json_cost, _ = await _settle(_json(payload), responses_api=True)

    stream_usage = _billed_usage(streaming_cost)
    json_usage = _billed_usage(json_cost)
    assert stream_usage is not None and json_usage is not None
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        assert stream_usage[field] == json_usage[field]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("input_shape", ["string", "message", "content_blocks"])
async def test_responses_estimate_includes_input(
    stream: bool, input_shape: str
) -> None:
    prompt = "Explain how payment reservations work. " * 100
    response_input: Any = prompt
    if input_shape == "message":
        response_input = [{"role": "user", "content": prompt}]
    elif input_shape == "content_blocks":
        response_input = [
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
        ]
    request_body = json.dumps(
        {
            "model": "gpt-4o",
            "instructions": "Answer concisely.",
            "input": response_input,
        }
    ).encode()
    payload = {
        "model": "gpt-4o",
        "output": [{"content": [{"type": "output_text", "text": "Hello"}]}],
    }
    response = _sse([payload]) if stream else _json(payload)
    _, get_cost, _ = await _settle(
        response, responses_api=True, request_body=request_body
    )

    usage = _billed_usage(get_cost)
    assert usage is not None
    assert usage["input_tokens"] > 100


@pytest.mark.asyncio
@pytest.mark.parametrize("responses_api", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tokens", [0, 17])
async def test_reported_usage_takes_precedence(
    responses_api: bool, stream: bool, tokens: int
) -> None:
    payload: dict[str, Any] = {
        "model": "gpt-4o",
        "usage": {"input_tokens": tokens, "output_tokens": tokens},
    }
    if responses_api:
        payload["output"] = [{"content": [{"type": "output_text", "text": "Hello"}]}]
    else:
        payload["choices"] = [{"message": {"content": "Hello"}}]

    response = _sse([payload]) if stream else _json(payload)
    _, get_cost, _ = await _settle(response, responses_api=responses_api)

    usage = _billed_usage(get_cost)
    assert usage is not None
    assert usage["input_tokens"] == tokens
    assert usage["output_tokens"] == tokens
    assert "estimated" not in usage


@pytest.mark.asyncio
@pytest.mark.parametrize("responses_api", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("unit", ["sat", "msat"])
async def test_pricing_error_refunds_full_prepayment(
    responses_api: bool, stream: bool, unit: str
) -> None:
    payload = {
        "model": "unpriced-model",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    response = _sse([payload]) if stream else _json(payload)
    with patch(
        "routstr.payment.cost_calculation._get_pricing_rates",
        side_effect=ValueError("No pricing for model"),
    ):
        result, _, send_refund = await _settle(
            response, responses_api=responses_api, unit=unit
        )

    send_refund.assert_awaited_once_with(10_000, unit, None, request_id=None)
    assert result.status_code == 200
    assert result.headers["X-Cashu"] == "cashuBrefund"
    assert result.headers["X-Routstr-Cost-Msats"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("responses_api", [False, True])
async def test_full_refund_is_logged_with_model_provider_and_body(
    responses_api: bool, caplog: pytest.LogCaptureFixture
) -> None:
    payload = {
        "model": "unpriced-model",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    base_logger = logging.getLogger("routstr.upstream.base")
    base_logger.addHandler(caplog.handler)
    try:
        with patch(
            "routstr.payment.cost_calculation._get_pricing_rates",
            side_effect=ValueError("No pricing for model"),
        ):
            await _settle(_json(payload), responses_api=responses_api)
    finally:
        base_logger.removeHandler(caplog.handler)

    record = next(
        r
        for r in caplog.records
        if r.getMessage() == "Zero-cost settlement, refunding the full prepayment"
    )
    logged = record.__dict__
    assert logged["model"] == "unpriced-model"
    assert logged["provider_type"] == "base"
    assert logged["upstream_base_url"] == "http://test"
    assert logged["refund_amount"] == 10_000
    assert logged["unit"] == "msat"
    assert "unpriced-model" in logged["response_body_preview"]


@pytest.mark.asyncio
async def test_pricing_failure_keeps_reported_usage_for_zero_charge_stats() -> None:
    from routstr.upstream.base import BaseUpstreamProvider

    provider = BaseUpstreamProvider("https://unused.example/v1", "unused", 1.0)
    with patch(
        "routstr.payment.cost_calculation._get_pricing_rates",
        side_effect=ValueError("No pricing for model"),
    ):
        cost = await provider.get_x_cashu_cost(
            {
                "model": "unpriced",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
            10_000,
            None,
        )
    assert cost is not None and cost.total_msats == 0
    assert (cost.input_tokens, cost.output_tokens) == (10, 5)
    assert cost.input_source == cost.output_source == "reported"
    assert cost.pricing_source == "missing"
