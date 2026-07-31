from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import StreamingResponse

from routstr import proxy as proxy_module


@pytest.mark.asyncio
async def test_proxy_closes_request_session_before_returning_response() -> None:
    """Route completion must release DB resources before response delivery."""
    request = MagicMock()
    request.method = "GET"
    request.headers = {"accept": "application/json"}
    request.url.path = "/not-an-api-route"
    request.state.request_id = "test-request"
    session = AsyncMock()

    response = await proxy_module.proxy(request, "not-an-api-route", session=session)

    assert response.status_code == 404
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_proxy_session_is_closed_before_first_stream_chunk() -> None:
    request = MagicMock()
    session = AsyncMock()

    async def stream() -> AsyncIterator[bytes]:
        session.close.assert_awaited_once()
        yield b"chunk"

    upstream_response = StreamingResponse(stream())
    with patch("routstr.proxy._proxy", AsyncMock(return_value=upstream_response)):
        response = await proxy_module.proxy(
            request, "v1/chat/completions", session=session
        )

    assert isinstance(response, StreamingResponse)
    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks == [b"chunk"]
