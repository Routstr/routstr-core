"""HTTP transport for the ``/v1/messages`` bridge.

Streams one upstream call over the shared per-origin httpx pool and turns
HTTP failures into ``UpstreamHTTPError`` with the message text litellm
1.101.2 surfaced (OpenAI-shaped error bodies are reduced to their
``message``; native Anthropic upstreams keep the raw body). OpenAI-SDK
providers retry 408/409/429/5xx twice, as the openai client did.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..core import get_logger
from .anthropic_openai_stream import StreamPayloadError
from .http_client import acquire_upstream_http_client

logger = get_logger(__name__)

STREAM_READ_TIMEOUT_SECONDS = 600.0
MAX_RETRIES = 2
_INITIAL_RETRY_DELAY = 0.5
_MAX_RETRY_DELAY = 8.0


class UpstreamHTTPError(Exception):
    """The upstream answered with an error status (or broke the stream)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        headers: httpx.Headers | dict[str, str] | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.headers = headers
        self.body = body


def error_message(body_text: str, *, raw: bool) -> str:
    """Upstream error text: the OpenAI ``error.message`` or the raw body."""
    if raw:
        return body_text
    try:
        payload = json.loads(body_text)
    except ValueError:
        return body_text
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(payload.get("message"), str):
            return payload["message"]
    return body_text


def _retry_delay(attempt: int, headers: httpx.Headers) -> float:
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            delay = float(value) * scale
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError):
                continue
        if 0 < delay <= 60:
            return delay
    delay = min(_INITIAL_RETRY_DELAY * 2**attempt, _MAX_RETRY_DELAY)
    return delay * (1 - 0.25 * random.random())


def _should_retry(response: httpx.Response) -> bool:
    should = response.headers.get("x-should-retry")
    if should in ("true", "false"):
        return should == "true"
    status = response.status_code
    return status in (408, 409, 429) or status >= 500


async def open_stream(
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    retries: int,
    raw_errors: bool,
    read_timeout: float = STREAM_READ_TIMEOUT_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> httpx.Response:
    """POST ``body`` and return the open streaming response.

    Raises ``UpstreamHTTPError`` for error statuses and lets httpx transport
    errors propagate after the retry budget is spent.
    """
    client = acquire_upstream_http_client(url)
    timeout = client.timeout
    for attempt in range(retries + 1):
        request = client.build_request(
            "POST",
            url,
            content=json.dumps(body).encode(),
            headers=headers,
            timeout=httpx.Timeout(
                connect=timeout.connect,
                read=read_timeout,
                write=timeout.write,
                pool=timeout.pool,
            ),
        )
        try:
            response = await client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            if attempt >= retries:
                raise
            await sleep(_retry_delay(attempt, httpx.Headers()))
            continue
        if response.status_code < 400:
            return response
        try:
            body_bytes = await response.aread()
        finally:
            await response.aclose()
        if attempt < retries and _should_retry(response):
            await sleep(_retry_delay(attempt, response.headers))
            continue
        text = body_bytes.decode("utf-8", errors="replace")
        raise UpstreamHTTPError(
            error_message(text, raw=raw_errors),
            status_code=response.status_code,
            headers=response.headers,
            body=text,
        )
    raise AssertionError("unreachable")


async def iter_sse_json(
    response: httpx.Response, *, skip_malformed: bool = False
) -> AsyncGenerator[dict, None]:
    """Yield the JSON objects of an SSE stream; stops at ``[DONE]``.

    A non-JSON event is a broken stream (litellm raised), not a gap, unless
    ``skip_malformed`` keeps the old Gemini translator's skipping.
    """
    parse = _loads if skip_malformed else _sse_payload
    data: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith(":"):
            continue
        if line:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            continue
        if not data:
            continue
        payload = "\n".join(data)
        data = []
        if payload.startswith("[DONE]"):
            return
        if (obj := parse(payload)) is not None:
            yield obj
    if data:
        payload = "\n".join(data)
        if not payload.startswith("[DONE]") and (obj := parse(payload)) is not None:
            yield obj


def _sse_payload(payload: str) -> dict | None:
    try:
        obj = json.loads(payload)
    except ValueError:
        raise StreamPayloadError(
            f"Malformed upstream event: {payload[:200]}", 500
        ) from None
    return obj if isinstance(obj, dict) else None


async def iter_ndjson(response: httpx.Response) -> AsyncGenerator[dict, None]:
    async for line in response.aiter_lines():
        if line.strip() and (obj := _loads(line)) is not None:
            yield obj


def _loads(payload: str) -> dict | None:
    try:
        obj = json.loads(payload)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class ResponseOwnedIterator:
    """Close the upstream response even if iteration never starts."""

    def __init__(self, iterator: AsyncIterator[Any], response: httpx.Response) -> None:
        self._iterator = iterator
        self._response = response
        self._cleanup_task: asyncio.Task[None] | None = None

    def __aiter__(self) -> ResponseOwnedIterator:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._iterator.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            raise
        except BaseException:
            try:
                await self.aclose()
            finally:
                raise

    async def _cleanup(self) -> None:
        try:
            close = getattr(self._iterator, "aclose", None)
            if close is not None:
                await close()
        finally:
            await self._response.aclose()

    async def aclose(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._cleanup_task)
