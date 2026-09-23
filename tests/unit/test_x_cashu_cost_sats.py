import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.core.terminal_outcomes import TerminalOutcomeContext  # noqa: E402
from routstr.payment.cost_calculation import CostData  # noqa: E402
from routstr.upstream.base import (  # noqa: E402
    BaseUpstreamProvider,
    _record_x_cashu_terminal_outcome,
)


def _make_provider() -> BaseUpstreamProvider:
    return BaseUpstreamProvider(base_url="http://test", api_key="test-key")


def _make_httpx_response(status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, headers={})


def _make_cost_data(total_msats: int = 5000) -> CostData:
    return CostData(
        base_msats=0,
        input_msats=3000,
        output_msats=2000,
        total_msats=total_msats,
        total_usd=0.00025,
        input_tokens=100,
        output_tokens=50,
        input_source="reported",
        output_source="reported",
    )


def test_zero_usage_x_cashu_preserves_captured_presence() -> None:
    record = MagicMock()
    context = TerminalOutcomeContext(
        outcome_id="zero-usage",
        model_identifier="author/model",
        input_source="reported",
        output_source="reported",
        cache_read_source="missing",
        cache_creation_source="missing",
    )

    with patch("routstr.upstream.base.record_terminal_outcome", record):
        _record_x_cashu_terminal_outcome(
            context,
            None,
            amount=10,
            unit="sat",
        )

    recorded_context = record.call_args.args[0]
    assert recorded_context.input_source == recorded_context.output_source == "reported"
    assert record.call_args.kwargs["revenue_msats"] == 10_000


# ---------------------------------------------------------------------------
# Non-streaming (chat completions)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_streaming_includes_cost_sats() -> None:
    provider = _make_provider()
    cost_data = _make_cost_data(total_msats=5000)

    response_body = {
        "model": "gpt-4o",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost": 0.00025,
        },
    }
    content_str = json.dumps(response_body)
    httpx_response = _make_httpx_response()

    with (
        patch.object(provider, "get_x_cashu_cost", new=AsyncMock(return_value=cost_data)),
        patch.object(provider, "send_refund", new=AsyncMock(return_value="cashuA_refund_token")),
    ):
        response = await provider.handle_x_cashu_non_streaming_response(
            content_str=content_str,
            response=httpx_response,
            amount=10000,
            unit="msat",
            max_cost_for_model=10000,
            mint=None,
        )

    body = json.loads(response.body)
    assert body["usage"]["cost_sats"] == 5  # 5000 msats // 1000
    assert body["usage"]["cost"]["total_msats"] == 5000
    assert body["usage"]["cost"]["input_msats"] == 3000
    assert body["usage"]["cost"]["output_msats"] == 2000
    assert response.headers["x-routstr-cost-msats"] == "5000"
    assert response.headers["x-routstr-input-cost-msats"] == "3000"
    assert response.headers["x-routstr-output-cost-msats"] == "2000"


@pytest.mark.asyncio
async def test_non_streaming_cost_sats_value_rounds_down() -> None:
    provider = _make_provider()
    cost_data = _make_cost_data(total_msats=1999)
    settlement_order: list[str] = []

    async def persist_refund(*args: object, **kwargs: object) -> str:
        settlement_order.append("refund")
        return "cashuA_refund_token"

    def record_outcome(*args: object, **kwargs: object) -> None:
        settlement_order.append("record")

    send_refund = AsyncMock(side_effect=persist_refund)
    record = MagicMock(side_effect=record_outcome)

    response_body = {"model": "gpt-4o", "usage": {"prompt_tokens": 10}}
    content_str = json.dumps(response_body)

    with (
        patch.object(provider, "get_x_cashu_cost", new=AsyncMock(return_value=cost_data)),
        patch.object(provider, "send_refund", new=send_refund),
        patch("routstr.upstream.base.record_terminal_outcome", record),
    ):
        response = await provider.handle_x_cashu_non_streaming_response(
            content_str=content_str,
            response=_make_httpx_response(),
            amount=10,
            unit="sat",
            max_cost_for_model=10000,
            request_id="sat-rounding-request",
        )

    body = json.loads(response.body)
    assert body["usage"]["cost_sats"] == 1  # 1999 // 1000
    send_refund.assert_awaited_once_with(
        8, "sat", None, request_id="sat-rounding-request"
    )
    record.assert_called_once()
    recorded_context = record.call_args.args[0]
    assert recorded_context.input_source == recorded_context.output_source == "reported"
    assert recorded_context.cache_read_source == "missing"
    assert recorded_context.cache_creation_source == "missing"
    assert record.call_args.kwargs["revenue_msats"] == 2000
    assert settlement_order == ["refund", "record"]


@pytest.mark.asyncio
async def test_non_streaming_preserves_tokens_and_replaces_upstream_cost() -> None:
    provider = _make_provider()
    cost_data = _make_cost_data(total_msats=3000)

    response_body = {
        "model": "gpt-4o",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost": 0.00015,
        },
    }

    with (
        patch.object(provider, "get_x_cashu_cost", new=AsyncMock(return_value=cost_data)),
        patch.object(provider, "send_refund", new=AsyncMock(return_value="cashuA_refund_token")),
    ):
        response = await provider.handle_x_cashu_non_streaming_response(
            content_str=json.dumps(response_body),
            response=_make_httpx_response(),
            amount=10000,
            unit="msat",
            max_cost_for_model=10000,
        )

    body = json.loads(response.body)
    usage = body["usage"]
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] == 150
    assert usage["cost"]["total_msats"] == 3000
    assert usage["cost"]["total_usd"] == 0.00025
    assert usage["cost_sats"] == 3


# ---------------------------------------------------------------------------
# Streaming (chat completions)
# ---------------------------------------------------------------------------

async def _collect_streaming(response: object) -> list[str]:
    chunks: list[str] = []
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode("utf-8"))
        else:
            chunks.append(str(chunk))
    return chunks


@pytest.mark.asyncio
async def test_streaming_includes_cost_sats_in_usage_chunk() -> None:
    provider = _make_provider()
    cost_data = _make_cost_data(total_msats=7000)

    usage_chunk = {
        "id": "chatcmpl-123",
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }
    content_str = "\n".join([
        'data: {"id":"chatcmpl-123","model":"gpt-4o","choices":[]}',
        f"data: {json.dumps(usage_chunk)}",
        "data: [DONE]",
    ])

    with patch.object(provider, "get_x_cashu_cost", new=AsyncMock(return_value=cost_data)):
        response = await provider.handle_x_cashu_streaming_response(
            content_str=content_str,
            response=_make_httpx_response(),
            amount=10000,
            unit="msat",
            max_cost_for_model=10000,
            mint=None,
        )

    chunks = await _collect_streaming(response)

    full_output = "".join(chunks)
    usage_line = next(
        line for line in full_output.split("\n") if '"usage"' in line and "cost_sats" in line
    )
    data_json = json.loads(usage_line.lstrip("data: ").strip())
    assert data_json["usage"]["cost_sats"] == 7  # 7000 // 1000


@pytest.mark.asyncio
async def test_streaming_non_usage_chunks_unmodified() -> None:
    provider = _make_provider()
    cost_data = _make_cost_data(total_msats=2000)

    regular_chunk = {"id": "chatcmpl-123", "model": "gpt-4o", "choices": [{"delta": {"content": "hi"}}]}
    usage_chunk = {"id": "chatcmpl-123", "model": "gpt-4o", "usage": {"prompt_tokens": 10}}
    content_str = "\n".join([
        f"data: {json.dumps(regular_chunk)}",
        f"data: {json.dumps(usage_chunk)}",
        "data: [DONE]",
    ])

    with patch.object(provider, "get_x_cashu_cost", new=AsyncMock(return_value=cost_data)):
        response = await provider.handle_x_cashu_streaming_response(
            content_str=content_str,
            response=_make_httpx_response(),
            amount=10000,
            unit="msat",
            max_cost_for_model=10000,
        )

    chunks = await _collect_streaming(response)

    lines = [
        line for line in "".join(chunks).split("\n")
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    regular_line_data = json.loads(lines[0][6:])
    # regular chunk should not have cost_sats injected
    assert "cost_sats" not in regular_line_data.get("usage", {})


@pytest.mark.asyncio
async def test_streaming_no_space_error_event_is_not_recorded() -> None:
    provider = _make_provider()
    record = MagicMock()

    with patch("routstr.upstream.base.record_terminal_outcome", record):
        await provider.handle_x_cashu_streaming_response(
            content_str='data:{"type":"error","error":{"message":"failed"}}\n\n',
            response=_make_httpx_response(),
            amount=10,
            unit="sat",
            max_cost_for_model=10_000,
            request_id="no-space-error",
        )

    record.assert_not_called()


@pytest.mark.asyncio
async def test_streaming_no_space_lines_keep_existing_billing_parse() -> None:
    provider = _make_provider()
    cost = AsyncMock(return_value=None)
    body = 'data:{"usage":{"prompt_tokens":100,"completion_tokens":50}}\n\n'

    with patch.object(provider, "get_x_cashu_cost", new=cost):
        response = await provider.handle_x_cashu_streaming_response(
            content_str=body,
            response=_make_httpx_response(),
            amount=10,
            unit="sat",
            max_cost_for_model=10_000,
            request_id="no-space-usage",
        )

    assert cost.await_args is not None
    assert cost.await_args.args[0]["usage"] is None
    assert "".join(await _collect_streaming(response)).strip() == body.strip()


@pytest.mark.asyncio
async def test_streaming_no_space_usage_is_still_recorded_as_reported() -> None:
    provider = _make_provider()
    record = MagicMock()
    body = 'data:{"usage":{"prompt_tokens":100,"completion_tokens":5}}\n\ndata:[DONE]\n\n'

    with (
        patch("routstr.upstream.base.record_terminal_outcome", record),
        patch.object(provider, "get_x_cashu_cost", new=AsyncMock(return_value=None)),
    ):
        await provider.handle_x_cashu_streaming_response(
            content_str=body,
            response=_make_httpx_response(),
            amount=10,
            unit="sat",
            max_cost_for_model=10_000,
            request_id="no-space-recorded",
        )

    context = record.call_args.args[0]
    assert (context.input_source, context.output_source) == ("reported", "reported")
    assert record.call_args.kwargs["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 5,
    }


@pytest.mark.asyncio
async def test_native_messages_stream_keeps_input_usage_in_stats() -> None:
    provider = _make_provider()
    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"model":"test-model","usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    writer = MagicMock()
    send_refund = AsyncMock(return_value="cashu-refund")
    with (
        patch("routstr.core.terminal_outcomes.terminal_outcome_writer", writer),
        patch.object(provider, "send_refund", send_refund),
        patch(
            "routstr.payment.cost_calculation._get_pricing_rates",
            return_value=(1_000.0, 1_000.0, 1_000.0, 1_000.0, "configured"),
        ),
        patch("routstr.payment.cost_calculation.sats_usd_price", return_value=0.0005),
    ):
        response = await provider.handle_x_cashu_streaming_response(
            content_str=body,
            response=_make_httpx_response(),
            amount=100,
            unit="msat",
            max_cost_for_model=100,
            request_id="messages-request",
        )
        await _collect_streaming(response)

    send_refund.assert_awaited_once_with(
        95, "msat", None, request_id="messages-request"
    )
    assert response.headers["x-cashu"] == "cashu-refund"
    writer.submit.assert_called_once()
    outcome = writer.submit.call_args.args[0]
    assert outcome.revenue_msats == 5
    assert (outcome.input_tokens, outcome.output_tokens) == (10, 5)
    assert outcome.input_source == outcome.output_source == "reported"
    writer.declare_loss.assert_not_called()
