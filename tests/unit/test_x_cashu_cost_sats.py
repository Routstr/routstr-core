import json
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.payment.cost_calculation import CostData  # noqa: E402
from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402


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
    )


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
            payment_token_hash=None,
        )

    body = json.loads(response.body)
    assert "cost_sats" in body["usage"]
    assert body["usage"]["cost_sats"] == 5  # 5000 msats // 1000


@pytest.mark.asyncio
async def test_non_streaming_cost_sats_value_rounds_down() -> None:
    provider = _make_provider()
    cost_data = _make_cost_data(total_msats=1999)

    response_body = {"model": "gpt-4o", "usage": {"prompt_tokens": 10}}
    content_str = json.dumps(response_body)

    with (
        patch.object(provider, "get_x_cashu_cost", new=AsyncMock(return_value=cost_data)),
        patch.object(provider, "send_refund", new=AsyncMock(return_value="cashuA_refund_token")),
    ):
        response = await provider.handle_x_cashu_non_streaming_response(
            content_str=content_str,
            response=_make_httpx_response(),
            amount=10000,
            unit="msat",
            max_cost_for_model=10000,
        )

    body = json.loads(response.body)
    assert body["usage"]["cost_sats"] == 1  # 1999 // 1000


@pytest.mark.asyncio
async def test_non_streaming_preserves_existing_usage_fields() -> None:
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
    assert usage["cost"] == 0.00015
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
            payment_token_hash=None,
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
