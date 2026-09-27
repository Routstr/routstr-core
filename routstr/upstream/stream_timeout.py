"""Timeout guards for upstream streaming responses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx

from ..core import get_logger
from ..core.error_scope import UPSTREAM_ERROR_STATUS
from ..core.exceptions import UpstreamError
from ..core.settings import settings

logger = get_logger(__name__)


async def open_guarded_stream(
    response: httpx.Response, provider_type: str
) -> AsyncIterator[bytes]:
    """Await the upstream's first chunk, then hand back the whole stream.

    Awaiting the first chunk before any ``StreamingResponse`` exists is what
    makes a slow-starting provider recoverable: the proxy's candidate loop only
    sees errors raised while it still owns the request, and no byte has reached
    the client yet. A stall after that chunk cannot fail over, so the returned
    iterator simply ends and the caller's finalizer settles actual usage.
    """
    chunks = response.aiter_bytes().__aiter__()
    timeout = settings.upstream_first_token_timeout_seconds
    try:
        first = await _next_chunk(chunks, timeout)
    except TimeoutError:
        await response.aclose()
        raise UpstreamError(
            f"Upstream {provider_type} sent no first chunk within {timeout}s",
            status_code=UPSTREAM_ERROR_STATUS,
            code="UPSTREAM_TIMEOUT",
        ) from None
    return _resume(first, chunks, provider_type)


async def _next_chunk(chunks: AsyncIterator[bytes], timeout: float) -> bytes | None:
    """Next chunk, or ``None`` at end of stream. ``timeout <= 0`` disables it."""
    step = anext(chunks)
    try:
        return await (asyncio.wait_for(step, timeout) if timeout > 0 else step)
    except StopAsyncIteration:
        return None


async def _resume(
    first: bytes | None, chunks: AsyncIterator[bytes], provider_type: str
) -> AsyncIterator[bytes]:
    idle_timeout = settings.upstream_stream_idle_timeout_seconds
    chunk = first
    while chunk is not None:
        yield chunk
        try:
            chunk = await _next_chunk(chunks, idle_timeout)
        except TimeoutError:
            logger.warning(
                "Upstream stream stalled; aborting and billing actual usage",
                extra={
                    "provider": provider_type,
                    "idle_timeout_seconds": idle_timeout,
                },
            )
            return
