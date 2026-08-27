import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from routstr.upstream.openrouter import OpenRouterUpstreamProvider


async def _body(response: Any) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_x_cashu_chat_stream_reports_complete_provider_path() -> None:
    provider = OpenRouterUpstreamProvider(api_key="test-key")
    payload = {"model": "glm-4.5", "provider": "z.ai"}
    content = f"data: {json.dumps(payload)}\n"

    response = await provider.handle_x_cashu_streaming_response(
        content,
        httpx.Response(200, headers={"content-type": "text/event-stream"}),
        amount=1,
        unit="sat",
        max_cost_for_model=1,
    )

    event = json.loads((await _body(response)).decode().removeprefix("data: "))
    assert event["provider"] == "openrouter:z.ai"


@pytest.mark.asyncio
async def test_x_cashu_responses_stream_reports_complete_provider_path() -> None:
    provider = OpenRouterUpstreamProvider(api_key="test-key")
    event = {"type": "response.created", "provider": "z.ai"}
    content = f"data: {json.dumps(event)}\n\n"

    with patch.object(
        provider, "get_x_cashu_cost", new=AsyncMock(return_value=None)
    ):
        response = await provider.handle_x_cashu_streaming_responses_response(
            content,
            httpx.Response(200, headers={"content-type": "text/event-stream"}),
            amount=1,
            unit="sat",
            max_cost_for_model=1,
        )

    payload = json.loads((await _body(response)).decode().removeprefix("data: "))
    assert payload["provider"] == "openrouter:z.ai"
