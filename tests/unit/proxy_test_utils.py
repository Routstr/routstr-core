"""Helpers for driving ``routstr.proxy.proxy`` with mocked request and session."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from routstr import proxy as proxy_module


def mock_request_stream(request: MagicMock, body: bytes) -> None:
    """Give a mocked request a readable body stream (the proxy reads the stream)."""

    async def stream() -> AsyncIterator[bytes]:
        yield body

    request.stream = stream


def patch_proxy_session(session: Any) -> Any:
    """Make the proxy route use ``session`` instead of opening its own."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        yield session

    return patch.object(proxy_module, "create_session", factory)
