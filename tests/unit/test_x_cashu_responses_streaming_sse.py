"""X-Cashu settlement for streaming ``/v1/responses``.

The stream is real SSE: CRLF delimiters, comment keepalives, ``event:`` fields,
multi-line ``data:`` payloads and a ``[DONE]`` sentinel. Canonical Responses API
usage arrives nested under ``response`` on ``response.completed``.
"""

import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.payment.cost_calculation import CostData  # noqa: E402
from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402


def _make_provider() -> BaseUpstreamProvider:
    return BaseUpstreamProvider(base_url="http://test", api_key="test-key")


def _make_cost_data(total_msats: int = 4000) -> CostData:
    return CostData(
        base_msats=0,
        input_msats=2500,
        output_msats=1500,
        total_msats=total_msats,
        total_usd=0.0002,
        input_tokens=12,
        output_tokens=8,
    )


def _sse_response(chunks: list[bytes]) -> httpx.Response:
    """Build the upstream response from wire chunks that split events."""
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=b"".join(chunks),
    )


COMPLETED_EVENT = {
    "type": "response.completed",
    "response": {
        "model": "gpt-5-mini",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 8,
            "total_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 3},
        },
    },
}


def _canonical_chunks() -> list[bytes]:
    """CRLF stream whose completed event straddles two wire chunks."""
    completed = json.dumps(COMPLETED_EVENT).encode()
    return [
        b": keepalive\r\n\r\n",
        b"event: response.created\r\n"
        b'data: {"type":"response.created","response":{"model":"gpt-5-mini"}}\r\n\r\n',
        b"event: response.completed\r\ndata: " + completed[:40],
        completed[40:] + b"\r\n\r\n",
        b"data: [DONE]\r\n\r\n",
    ]


async def _collect(response: Any) -> bytes:
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
    return body


async def _settle(
    chunks: list[bytes],
    *,
    amount: int = 10_000,
    max_cost_for_model: int = 9_000,
    cost_data: CostData | None = None,
) -> tuple[Any, AsyncMock, AsyncMock]:
    provider = _make_provider()
    get_cost = (
        AsyncMock(return_value=cost_data)
        if cost_data is not None
        else AsyncMock(side_effect=provider.get_x_cashu_cost)
    )
    send_refund = AsyncMock(return_value="cashuBrefundtoken0123456789")
    with (
        patch.object(provider, "get_x_cashu_cost", new=get_cost),
        patch.object(provider, "send_refund", new=send_refund),
    ):
        response = await provider.handle_x_cashu_responses_completion(
            response=_sse_response(chunks),
            amount=amount,
            unit="msat",
            max_cost_for_model=max_cost_for_model,
            mint=None,
        )
    return response, get_cost, send_refund


@pytest.mark.asyncio
async def test_fragmented_crlf_stream_refunds_and_sets_cost_headers() -> None:
    response, _, send_refund = await _settle(
        _canonical_chunks(), cost_data=_make_cost_data(4000)
    )

    send_refund.assert_awaited_once()
    assert send_refund.await_args is not None
    assert send_refund.await_args.args[0] == 10_000 - 4000
    assert response.headers["x-cashu"] == "cashuBrefundtoken0123456789"
    assert response.headers["x-routstr-cost-msats"] == "4000"
    assert response.headers["x-routstr-input-cost-msats"] == "2500"
    assert response.headers["x-routstr-output-cost-msats"] == "1500"


@pytest.mark.asyncio
async def test_nested_completion_usage_drives_cost_calculation() -> None:
    _, get_cost, _ = await _settle(_canonical_chunks(), cost_data=_make_cost_data(4000))

    assert get_cost.await_args is not None
    response_data = get_cost.await_args.args[0]
    assert response_data["model"] == "gpt-5-mini"
    assert response_data["usage"]["input_tokens"] == 12
    assert response_data["usage"]["output_tokens"] == 8


@pytest.mark.asyncio
async def test_reemitted_stream_is_valid_sse() -> None:
    response, _, _ = await _settle(_canonical_chunks(), cost_data=_make_cost_data(4000))
    body = await _collect(response)

    assert b"\\n" not in body
    assert body.endswith(b"\n\n")
    assert b": keepalive" not in body

    events = [e for e in body.split(b"\n\n") if e.strip()]
    payloads = []
    for event in events:
        data_lines = [
            line[len(b"data:") :].lstrip()
            for line in event.split(b"\n")
            if line.startswith(b"data:")
        ]
        assert data_lines, f"event carries no data line: {event!r}"
        payloads.append(b"\n".join(data_lines))

    assert payloads[-1] == b"[DONE]"
    assert any(b"event: response.completed" in event for event in events)

    completed = json.loads(payloads[-2])
    assert completed["type"] == "response.completed"
    assert completed["response"]["usage"]["cost"]["total_msats"] == 4000


@pytest.mark.asyncio
async def test_multiline_data_payload_is_parsed_and_reframed() -> None:
    completed = json.dumps(COMPLETED_EVENT)
    head, tail = completed[:30], completed[30:]
    chunks = [
        ("data: " + head + "\r\ndata: " + tail + "\r\n\r\n").encode(),
        b"data: [DONE]\r\n\r\n",
    ]

    response, get_cost, send_refund = await _settle(
        chunks, cost_data=_make_cost_data(4000)
    )

    assert get_cost.await_args is not None
    assert send_refund.await_args is not None
    assert get_cost.await_args.args[0]["usage"]["input_tokens"] == 12
    assert send_refund.await_args.args[0] == 6000
    body = await _collect(response)
    for event in body.split(b"\n\n"):
        for line in event.split(b"\n"):
            if line.strip():
                assert line.startswith(b"data:") or line.startswith(b"event:")


@pytest.mark.asyncio
async def test_missing_usage_settles_at_authorized_max() -> None:
    chunks = [
        b'data: {"type":"response.created","response":{"model":"gpt-5-mini"}}\r\n\r\n',
        b"data: [DONE]\r\n\r\n",
    ]

    response, _, send_refund = await _settle(
        chunks, amount=10_000, max_cost_for_model=9_000
    )

    send_refund.assert_awaited_once()
    assert send_refund.await_args is not None
    assert send_refund.await_args.args[0] == 10_000 - 9_000
    assert response.headers["x-cashu"] == "cashuBrefundtoken0123456789"
    assert response.headers["x-routstr-cost-msats"] == "9000"


@pytest.mark.asyncio
async def test_malformed_events_do_not_retain_whole_token() -> None:
    chunks = [
        b"data: {not json\r\n\r\n",
        b"data: [DONE]\r\n\r\n",
    ]

    response, _, send_refund = await _settle(
        chunks, amount=10_000, max_cost_for_model=9_000
    )

    assert send_refund.await_args is not None
    assert send_refund.await_args.args[0] == 1000
    body = await _collect(response)
    assert b"\\n" not in body
    assert body.endswith(b"\n\n")
