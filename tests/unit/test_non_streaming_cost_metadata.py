import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from routstr.core.db import ApiKey
from routstr.upstream import base
from routstr.upstream.base import BaseUpstreamProvider


@pytest.mark.asyncio
async def test_non_streaming_deepseek_uses_shared_cost_metadata_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-streaming responses repair and expose their component costs."""
    provider = BaseUpstreamProvider(
        base_url="https://api.deepseek.com", api_key="test-key"
    )
    key = MagicMock(spec=ApiKey)
    key.hashed_key = "test-hash"
    key.balance = 10_000
    session = MagicMock()
    session.refresh = AsyncMock()
    response = httpx.Response(
        200,
        json={
            "id": "chatcmpl-deepseek",
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {
                "prompt_tokens": 82,
                "completion_tokens": 121,
                "total_tokens": 203,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 115},
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 82,
            },
        },
    )
    monkeypatch.setattr(
        base,
        "adjust_payment_for_tokens",
        AsyncMock(
            return_value={
                "base_msats": 100,
                "input_msats": 0,
                "output_msats": 0,
                "total_msats": 100,
                "total_usd": 0.00006,
                "input_tokens": 82,
                "output_tokens": 121,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
        ),
    )

    result = await provider.handle_non_streaming_chat_completion(
        response=response,
        key=key,
        session=session,
        deducted_max_cost=100,
        requested_model="deepseek-v4-flash",
    )
    payload = json.loads(result.body)

    assert payload["cost"]["input_msats"] == 41
    assert payload["cost"]["output_msats"] == 59
    assert payload["cost"]["total_msats"] == 100
    assert payload["metadata"]["routstr"]["cost"]["input_msats"] == 41
    assert payload["usage"]["cost_sats"] == 0
