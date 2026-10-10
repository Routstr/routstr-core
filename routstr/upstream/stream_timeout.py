"""Timeout guards for upstream streaming responses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx

from ..core import get_logger
from ..core.error_scope import UPSTREAM_ERROR_STATUS
from ..core.exceptions import UpstreamError
from ..core.settings import settings
from .sse_splitter import SSEEventSplitter

logger = get_logger(__name__)


class GuardedStream(AsyncIterator[bytes]):
    def __init__(
        self,
        first: bytes | None,
        chunks: AsyncIterator[bytes],
        provider_type: str,
        on_idle_timeout: Callable[[], None] | None,
    ) -> None:
        self.timed_out = False
        self._chunks = self._resume(first, chunks, provider_type, on_idle_timeout)

    def __aiter__(self) -> GuardedStream:
        return self

    async def __anext__(self) -> bytes:
        return await anext(self._chunks)

    async def _resume(
        self,
        first: bytes | None,
        chunks: AsyncIterator[bytes],
        provider_type: str,
        on_idle_timeout: Callable[[], None] | None,
    ) -> AsyncIterator[bytes]:
        chunk = first
        while chunk is not None:
            yield chunk
            try:
                chunk = await _next_chunk(
                    chunks, settings.upstream_stream_idle_timeout_seconds
                )
            except TimeoutError:
                self.timed_out = True
                logger.warning(
                    "Upstream stream stalled; aborting and billing actual usage",
                    extra={
                        "provider": provider_type,
                        "idle_timeout_seconds": settings.upstream_stream_idle_timeout_seconds,
                    },
                )
                if on_idle_timeout is not None:
                    on_idle_timeout()
                return


def _has_data(event: bytes) -> bool:
    return any(
        line.startswith(b"data:") and line[5:].strip() for line in event.split(b"\n")
    )


async def _sse_events(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Yield only deliverable SSE data events; comments cannot reset deadlines."""
    splitter = SSEEventSplitter()
    async for chunk in chunks:
        for event in splitter.feed(chunk):
            if _has_data(event):
                yield event + b"\n\n"
    tail = splitter.flush()
    if _has_data(tail):
        # Keep an unterminated tail unterminated: the caller's final flush must
        # not mistake truncated JSON for a complete SSE frame.
        yield tail


async def open_guarded_stream(
    response: httpx.Response,
    provider_type: str,
    *,
    sse: bool = False,
    on_idle_timeout: Callable[[], None] | None = None,
) -> GuardedStream:
    """Prefetch a deliverable event before handing a response to the client.

    Once the first event is sent, a stall cannot fail over; the stream ends and
    the caller's finalizer settles usage observed before the interruption.
    """
    chunks = response.aiter_bytes().__aiter__()
    guarded_chunks = _sse_events(chunks) if sse else chunks
    timeout = settings.upstream_first_token_timeout_seconds
    try:
        first = await _next_chunk(guarded_chunks, timeout)
    except TimeoutError:
        await response.aclose()
        raise UpstreamError(
            f"Upstream {provider_type} sent no first chunk within {timeout}s",
            status_code=UPSTREAM_ERROR_STATUS,
            code="UPSTREAM_TIMEOUT",
        ) from None
    return GuardedStream(first, guarded_chunks, provider_type, on_idle_timeout)


async def _next_chunk(chunks: AsyncIterator[bytes], timeout: float) -> bytes | None:
    """Next chunk, or ``None`` at end of stream. ``timeout <= 0`` disables it."""
    step = anext(chunks)
    try:
        return await (asyncio.wait_for(step, timeout) if timeout > 0 else step)
    except StopAsyncIteration:
        return None
