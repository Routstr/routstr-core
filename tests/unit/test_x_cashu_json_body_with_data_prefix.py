import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.payment.cost_calculation import CostData  # noqa: E402
from routstr.upstream.base import BaseUpstreamProvider, _is_sse_body  # noqa: E402

REFUND_TOKEN = "cashuBrefundtoken0123456789"


def _cost_data() -> CostData:
    return CostData(
        base_msats=0,
        input_msats=2500,
        output_msats=1500,
        total_msats=4000,
        total_usd=0.0002,
        input_tokens=12,
        output_tokens=8,
    )


def _chat_json_with_data_prefix() -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "gpt-5-mini",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Here you go: data:image/png;base64,iVBORw0KGgo=",
                },
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }


def _responses_json_with_data_prefix() -> dict[str, Any]:
    return {
        "id": "resp-1",
        "object": "response",
        "model": "gpt-5-mini",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "use data: prefix"}],
            }
        ],
        "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
    }


def _json_response(payload: dict[str, Any], content_type: str | None) -> httpx.Response:
    headers = {"content-type": content_type} if content_type else {}
    return httpx.Response(200, headers=headers, content=json.dumps(payload).encode())


async def _settle_chat(response: httpx.Response) -> tuple[Any, AsyncMock]:
    provider = BaseUpstreamProvider(base_url="http://test", api_key="test-key")
    send_refund = AsyncMock(return_value=REFUND_TOKEN)
    with (
        patch.object(
            provider, "get_x_cashu_cost", new=AsyncMock(return_value=_cost_data())
        ),
        patch.object(provider, "send_refund", new=send_refund),
    ):
        result = await provider.handle_x_cashu_chat_completion(
            response=response,
            amount=10_000,
            unit="msat",
            max_cost_for_model=9_000,
            mint=None,
        )
    return result, send_refund


async def _settle_responses(response: httpx.Response) -> tuple[Any, AsyncMock]:
    provider = BaseUpstreamProvider(base_url="http://test", api_key="test-key")
    send_refund = AsyncMock(return_value=REFUND_TOKEN)
    with (
        patch.object(
            provider, "get_x_cashu_cost", new=AsyncMock(return_value=_cost_data())
        ),
        patch.object(provider, "send_refund", new=send_refund),
    ):
        result = await provider.handle_x_cashu_responses_completion(
            response=response,
            amount=10_000,
            unit="msat",
            max_cost_for_model=9_000,
            mint=None,
        )
    return result, send_refund


@pytest.mark.asyncio
async def test_chat_json_with_data_prefix_is_not_streaming() -> None:
    response = _json_response(_chat_json_with_data_prefix(), "application/json")
    result, send_refund = await _settle_chat(response)

    send_refund.assert_awaited_once()
    assert send_refund.await_args is not None
    assert send_refund.await_args.args[0] == 6000
    assert result.headers["x-cashu"] == REFUND_TOKEN
    assert result.headers["x-routstr-cost-msats"] == "4000"
    body = json.loads(bytes(result.body))
    assert body["usage"]["cost"]["total_msats"] == 4000
    assert "data:image/png" in body["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_responses_json_with_data_prefix_is_not_streaming() -> None:
    response = _json_response(_responses_json_with_data_prefix(), None)
    result, send_refund = await _settle_responses(response)

    send_refund.assert_awaited_once()
    assert send_refund.await_args is not None
    assert send_refund.await_args.args[0] == 6000
    assert result.headers["x-cashu"] == REFUND_TOKEN
    body = json.loads(bytes(result.body))
    assert body["usage"]["cost"]["total_msats"] == 4000


@pytest.mark.asyncio
async def test_event_stream_content_type_is_streaming() -> None:
    chunk = json.dumps(
        {
            "model": "gpt-5-mini",
            "choices": [{"delta": {"content": "hi"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8},
        }
    )
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"data: {chunk}\n\ndata: [DONE]\n\n".encode(),
    )

    result, send_refund = await _settle_chat(response)

    send_refund.assert_awaited_once()
    assert result.headers["x-cashu"] == REFUND_TOKEN
    assert hasattr(result, "body_iterator")


@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        ("text/event-stream", '{"a": 1}', True),
        ("application/json", "data: {}\n\n", False),
        ("application/json; charset=utf-8", 'data: "x"', False),
        (None, '{"content": "data:image/png;base64,AAAA"}', False),
        (None, "data: {}\n\n", True),
        (None, "\n\n: keepalive\n\ndata: {}\n\n", True),
        (None, "event: x\ndata: {}\n\n", True),
        (None, '﻿{"data:": 1}', False),
        ("text/plain", "data: {}\n\n", True),
        ("text/plain", '{"x": "data:"}', False),
        (None, "", False),
    ],
)
def test_is_sse_body(content_type: str | None, body: str, expected: bool) -> None:
    assert _is_sse_body(content_type, body) is expected
