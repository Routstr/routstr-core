"""Bounded request-body read: size cap, read timeout, and late DB session."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import Response

from routstr import proxy as proxy_module
from routstr.core.settings import settings


def _make_request(headers: dict[str, str], chunks: list[bytes]) -> MagicMock:
    request = MagicMock()
    request.method = "POST"
    request.headers = headers
    request.state.request_id = "req-bounded-body"
    request.consumed = []

    async def stream() -> AsyncIterator[bytes]:
        for chunk in chunks:
            request.consumed.append(chunk)
            yield chunk

    request.stream = stream
    return request


def _slow_request(delay: float) -> MagicMock:
    request = MagicMock()
    request.method = "POST"
    request.headers = {}
    request.state.request_id = "req-slow-body"

    async def stream() -> AsyncIterator[bytes]:
        yield b"{"
        await asyncio.sleep(delay)
        yield b"}"

    request.stream = stream
    return request


async def _run(request: MagicMock) -> tuple[Any, MagicMock, AsyncMock]:
    """Run the proxy route with the session factory and _proxy stubbed out."""
    session_factory = MagicMock()
    inner = AsyncMock(return_value=Response(status_code=200))
    with (
        patch.object(proxy_module, "create_session", session_factory),
        patch.object(proxy_module, "_proxy", inner),
    ):
        response = await proxy_module.proxy(request, "v1/chat/completions")
    return response, session_factory, inner


@pytest.mark.asyncio
async def test_oversize_content_length_rejected_without_reading() -> None:
    request = _make_request({"content-length": "999999999"}, [b"x" * 16])

    response, session_factory, inner = await _run(request)

    assert response.status_code == 413
    assert request.consumed == []
    inner.assert_not_awaited()
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_oversize_chunked_body_rejected_mid_stream() -> None:
    with patch.object(settings, "max_request_body_bytes", 8):
        request = _make_request({}, [b"1234", b"5678", b"9012", b"3456"])
        response, session_factory, inner = await _run(request)

    assert response.status_code == 413
    # Reading stops as soon as the cap is exceeded; the last chunk is never read.
    assert request.consumed == [b"1234", b"5678", b"9012"]
    inner.assert_not_awaited()
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_slow_body_times_out() -> None:
    with patch.object(settings, "request_body_timeout_seconds", 0.05):
        request = _slow_request(delay=5)
        response, session_factory, inner = await _run(request)

    assert response.status_code == 408
    inner.assert_not_awaited()
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_normal_request_reaches_proxy_with_body() -> None:
    body = b'{"model": "test-model"}'
    request = _make_request({"content-length": str(len(body))}, [body])

    response, session_factory, inner = await _run(request)

    assert response.status_code == 200
    session_factory.assert_called_once()
    inner.assert_awaited_once_with(request, "v1/chat/completions", ANY, body)
