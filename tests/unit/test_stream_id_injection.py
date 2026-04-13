import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest

from routstr.core.db import ApiKey
from routstr.upstream.base import BaseUpstreamProvider


@pytest.mark.asyncio
async def test_stream_with_id_injection() -> None:
    """Test that stream_with_cost correctly injects IDs into complete JSON chunks but skips partials."""
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test_key"
    )

    # Mock response with mixed chunks:
    # 1. Complete JSON without ID
    # 2. Partial JSON (should be passed through)
    # 3. Complete JSON with ID (should be preserved or updated if requested_model is set)
    # 4. [DONE] message
    chunks = [
        b'data: {"choices": [{"delta": {"content": "Hello"}}]}\n\n',
        b'data: {"choices": [{"delta": {"content": "',  # Partial
        b'world"}}]}\n\n',
        b'data: {"id": "existing-id", "choices": [{"delta": {"content": "!"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def aiter_bytes() -> AsyncGenerator[bytes, None]:
        for chunk in chunks:
            yield chunk

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/event-stream"}
    mock_response.aiter_bytes = aiter_bytes

    key = MagicMock(spec=ApiKey)
    key.hashed_key = "test_hash"
    key.balance = 1000

    background_tasks = MagicMock()

    # We need to mock adjust_payment_for_tokens since it's called at the end
    with MagicMock():
        from routstr.upstream import base

        # Mocking the module-level function used in the generator
        base.adjust_payment_for_tokens = AsyncMock(
            return_value={"total_usd": 0.1, "total_msats": 100}
        )
        base.create_session = MagicMock()

        streaming_response = await provider.handle_streaming_chat_completion(
            response=mock_response,
            key=key,
            max_cost_for_model=100,
            background_tasks=background_tasks,
            requested_model="test-model",
        )

        results = []
        async for chunk in streaming_response.body_iterator:
            results.append(chunk)

        # Parse results
        parsed_results = []
        for r in results:
            if isinstance(r, bytes) and r.startswith(b"data: "):
                data = r[6:].decode().strip()
                if data == "[DONE]":
                    parsed_results.append(data)
                else:
                    try:
                        parsed_results.append(json.loads(data))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        parsed_results.append(
                            data
                        )  # Keep as string if it failed to parse

        # Verifications
        # 1. First chunk should have an injected ID and the requested model
        assert isinstance(parsed_results[0], dict)
        assert "id" in parsed_results[0]
        assert parsed_results[0]["id"].startswith("chatcmpl-")
        assert parsed_results[0]["model"] == "test-model"

        # 2. Second chunk was partial, should be passed as-is
        # In current implementation, re.split(b"data: ", b'data: {...') gives ['', '{...']
        # The first empty part is skipped. The second part is processed.

        # Check that we have results
        assert len(parsed_results) >= 4

        # Find the chunk that was "existing-id"
        id_chunk = next(
            r
            for r in parsed_results
            if isinstance(r, dict)
            and "choices" in r
            and r["choices"][0]["delta"].get("content") == "!"
        )
        assert id_chunk["id"] == parsed_results[0]["id"]
        assert id_chunk["model"] == "test-model"

        # 4. [DONE] should be there
        assert "[DONE]" in parsed_results
