from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Self, cast

import httpx
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from ..core import get_logger

logger = get_logger(__name__)


async def aclose_if_needed(resource: object | None) -> None:
    if resource is None:
        return
    close = getattr(resource, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def shielded_aclose(resource: object | None) -> None:
    await asyncio.shield(aclose_if_needed(resource))


class ResponseHandoff:
    """Close a response unless ownership is transferred to a stream."""

    def __init__(self) -> None:
        self._response: object | None = None

    def acquire(self, response: object) -> None:
        self._response = response

    def handoff(self) -> None:
        self._response = None

    async def close(self, *, suppress_errors: bool = False) -> None:
        response = self._response
        self._response = None
        if response is None:
            return
        try:
            await shielded_aclose(response)
        except BaseException:
            if not suppress_errors:
                raise
            logger.exception("Failed to close upstream response before handoff")


async def finalize_and_close_stream(
    finalize: Callable[[], Awaitable[None]] | None,
    response: object | None,
) -> None:
    """Settle billing, then return the response connection to its pool."""
    try:
        if finalize is not None:
            await finalize()
    finally:
        await aclose_if_needed(response)


class PersistentStreamFinalizer:
    """Run one stream finalizer to completion across cancellation boundaries."""

    def __init__(self, finalize: Callable[[], Awaitable[None]]) -> None:
        self._finalize = finalize
        self._task: asyncio.Future[None] | None = None
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        async with self._lock:
            if self._task is None:
                self._task = asyncio.ensure_future(self._finalize())
            task = self._task
        await asyncio.shield(task)


class FinalizingAsyncIterator:
    """Tie iterator shutdown to a finalizer created before streaming starts."""

    def __init__(
        self,
        iterator: AsyncIterator[bytes],
        finalizer: PersistentStreamFinalizer,
    ) -> None:
        self._iterator = iterator
        self._finalizer = finalizer

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._iterator.__anext__()
        except BaseException:
            await self._finalizer.run()
            raise

    async def aclose(self) -> None:
        try:
            await aclose_if_needed(self._iterator)
        finally:
            await self._finalizer.run()


class ClosingStreamingResponse(StreamingResponse):
    """Close the body iterator even when downstream ASGI sends fail."""

    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        finalizer: PersistentStreamFinalizer | None = None,
        **kwargs: Any,
    ) -> None:
        if finalizer is not None:
            content = FinalizingAsyncIterator(content, finalizer)
        super().__init__(content, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.shield(aclose_if_needed(self.body_iterator))


class OwnedUpstreamStream:
    """Keep a one-shot HTTP client alive for the lifetime of its response."""

    def __init__(
        self,
        iterator: AsyncIterator[bytes],
        response: httpx.Response,
        client: httpx.AsyncClient,
    ) -> None:
        self._iterator = iterator
        self._response = response
        self._client = client
        self._cleanup_complete = False
        self._cleanup_task: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._iterator.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            raise

    async def _cleanup(self) -> None:
        try:
            await aclose_if_needed(self._iterator)
        finally:
            try:
                await self._response.aclose()
            finally:
                await self._client.aclose()
        self._cleanup_complete = True

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._cleanup_complete:
                return
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._cleanup())
            cleanup_task = self._cleanup_task
        await asyncio.shield(cleanup_task)


def attach_upstream_stream_owner(
    result: StreamingResponse,
    response: httpx.Response,
    client: httpx.AsyncClient,
) -> StreamingResponse:
    result.body_iterator = OwnedUpstreamStream(
        cast(AsyncIterator[bytes], result.body_iterator), response, client
    )
    return result


async def close_upstream_exchange(
    response: httpx.Response | None, client: httpx.AsyncClient
) -> None:
    try:
        if response is not None:
            await response.aclose()
    finally:
        await client.aclose()
