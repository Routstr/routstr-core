"""h11-based HTTP client for EHBP requests that captures HTTP trailers.

httpx/httpcore silently discard HTTP trailers during chunked transfer
decoding. Tinfoil returns ``X-Tinfoil-Usage-Metrics`` as a trailer on
streaming responses, so we need a lower-level HTTP client that preserves
trailers from the h11 ``EndOfMessage`` event.

EHBP response bodies are opaque encrypted blobs, but that does **not**
require buffering them. The EHBP client decrypts the body with a streaming
``ReadableStream`` (one frame per encrypted chunk), so forwarding each
ciphertext chunk as it arrives gives the caller real time-to-first-token.
``stream_with_trailer`` does exactly that, exposing usage/trailer headers
only once the body completes. ``forward_with_trailer`` remains the buffered
variant for error paths that must inspect the whole body before replying.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

import h11

from ..core import get_logger
from ..core.exceptions import EhbpConnectionError, EhbpTimeoutError

logger = get_logger(__name__)

_READ_BUFSIZE = 65536
_DEFAULT_TIMEOUT_SECONDS = 600.0
_DEFAULT_CLOSE_TIMEOUT_SECONDS = 1.0
_DEFAULT_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass
class TrailerResponse:
    """Buffered HTTP response with optional trailer headers."""

    status_code: int
    headers: list[tuple[str, str]]
    body: bytes
    trailers: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class StreamedTrailerResponse:
    """Response whose body is still arriving, plus a trailers future.

    ``body`` yields ciphertext as the upstream produces it. ``trailers``
    resolves once the body is exhausted, mirroring ``TrailerResponse.trailers``
    for callers that need usage metadata before settling payment.
    """

    status_code: int
    headers: list[tuple[str, str]]
    body: AsyncIterator[bytes]
    trailers: "asyncio.Future[list[tuple[str, str]]]"
    _on_abandon: "Callable[[], None]" = field(default=lambda: None)

    async def aclose(self) -> None:
        """Stop reading and release the upstream connection immediately."""
        self._on_abandon()
        await self.body.aclose()  # type: ignore[attr-defined]


def _get_header(headers: list[tuple[str, str]], name: str) -> str | None:
    name_lower = name.lower()
    for k, v in headers:
        if k.lower() == name_lower:
            return v
    return None


def _strip_hop_by_hop_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove connection-specific headers before serializing a new request."""
    connection_tokens: set[str] = set()
    for key, value in headers.items():
        if key.lower() == "connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )

    excluded = _HOP_BY_HOP_HEADERS | connection_tokens
    return {key: value for key, value in headers.items() if key.lower() not in excluded}


async def _connect_and_send(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout_seconds: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, h11.Connection, str]:
    """Open the TLS connection and write the request, sharing the error mapping.

    Returns the reader/writer, an h11 client connection ready to parse the
    response, and the host (for timeout messages).
    """
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"Invalid URL (no hostname): {url}")
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    # FastAPI has already decoded the incoming request body. Do not carry the
    # original connection's framing or other hop-by-hop metadata into the new
    # upstream connection.
    headers = _strip_hop_by_hop_headers(headers)

    ssl_ctx = ssl.create_default_context()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_ctx),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise EhbpTimeoutError(
            f"EHBP upstream {host} timed out after {timeout_seconds:g}s connecting"
        ) from exc
    except ConnectionAbortedError as exc:
        # CPython's ssl module aborts a TLS handshake that outlives its
        # internal timer ("SSL handshake is taking longer than N seconds")
        # with ConnectionAbortedError. That is a connect timeout on the
        # provider hop, not a local node fault, so surface it as a timeout.
        raise EhbpTimeoutError(
            f"EHBP upstream {host} TLS handshake timed out while connecting"
        ) from exc
    except (ssl.SSLError, ConnectionError, OSError) as exc:
        # DNS failure, connection refused/reset, or a non-timeout TLS error:
        # the provider could not be reached. Attribute it to the upstream hop
        # rather than letting it become a node-scoped 500.
        raise EhbpConnectionError(
            f"Unable to connect to EHBP upstream {host}: {type(exc).__name__}"
        ) from exc

    # Build HTTP/1.1 request
    header_lines = [f"{method} {path} HTTP/1.1"]
    has_host = any(k.lower() == "host" for k in headers)
    if not has_host:
        header_lines.append(f"Host: {host}")
    header_lines.append("Connection: close")

    for key, value in headers.items():
        if key.lower() == "host":
            continue
        header_lines.append(f"{key}: {value}")

    if body and not any(k.lower() == "content-length" for k in headers):
        header_lines.append(f"Content-Length: {len(body)}")

    request_data = "\r\n".join(header_lines).encode() + b"\r\n\r\n"
    if body:
        request_data += body

    writer.write(request_data)
    try:
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        _close_writer(writer)
        raise EhbpTimeoutError(
            f"EHBP upstream {host} timed out after {timeout_seconds:g}s sending request"
        ) from exc

    return reader, writer, h11.Connection(h11.CLIENT), host


def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()


async def _close_writer_and_wait(
    writer: asyncio.StreamWriter, close_timeout_seconds: float
) -> None:
    _close_writer(writer)
    if close_timeout_seconds > 0:
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=close_timeout_seconds)
        except Exception:
            pass


async def _read_event(
    conn: h11.Connection,
    reader: asyncio.StreamReader,
    *,
    host: str,
    timeout_seconds: float,
) -> "h11.Event | type[h11.NEED_DATA] | type[h11.PAUSED]":
    """Next h11 event, pulling more bytes from the socket when needed."""
    while True:
        event = conn.next_event()
        if event is not h11.NEED_DATA:
            return event
        try:
            data = await asyncio.wait_for(
                reader.read(_READ_BUFSIZE), timeout=timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise EhbpTimeoutError(
                f"EHBP upstream {host} timed out after "
                f"{timeout_seconds:g}s waiting for response data"
            ) from exc
        conn.receive_data(data if data else b"")


async def forward_with_trailer(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    close_timeout_seconds: float = _DEFAULT_CLOSE_TIMEOUT_SECONDS,
) -> TrailerResponse:
    """Send an HTTP/1.1 request via h11 and capture HTTP trailers.

    Returns a :class:`TrailerResponse` with the full buffered body and any
    trailer headers from the ``EndOfMessage`` event. Use this only when the
    whole body is needed before replying (error paths); it withholds every
    byte until the upstream finishes. ``stream_with_trailer`` streams instead.
    """
    reader, writer, conn, host = await _connect_and_send(
        method=method,
        url=url,
        headers=headers,
        body=body,
        timeout_seconds=timeout_seconds,
    )

    try:
        status_code = 0
        resp_headers: list[tuple[str, str]] = []
        body_chunks: list[bytes] = []
        body_size = 0
        trailers: list[tuple[str, str]] = []

        while True:
            event = await _read_event(
                conn, reader, host=host, timeout_seconds=timeout_seconds
            )

            if isinstance(event, h11.Response):
                status_code = event.status_code
                resp_headers = [(k.decode(), v.decode()) for k, v in event.headers]

            elif isinstance(event, h11.Data):
                body_size += len(event.data)
                if body_size > max_response_bytes:
                    raise ValueError(
                        f"EHBP response exceeded {max_response_bytes} bytes"
                    )
                body_chunks.append(event.data)

            elif isinstance(event, h11.EndOfMessage):
                for k, v in event.headers:
                    trailers.append((k.decode(), v.decode()))
                break

            elif isinstance(event, h11.PAUSED):
                # Shouldn't happen for simple request/response, but break safely
                logger.warning("h11 PAUSED event during EHBP response parsing")
                break

            elif isinstance(event, h11.ConnectionClosed):
                break

        return TrailerResponse(
            status_code=status_code,
            headers=resp_headers,
            body=b"".join(body_chunks),
            trailers=trailers,
        )
    finally:
        await _close_writer_and_wait(writer, close_timeout_seconds)


async def stream_with_trailer(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    close_timeout_seconds: float = _DEFAULT_CLOSE_TIMEOUT_SECONDS,
) -> StreamedTrailerResponse:
    """Send an HTTP/1.1 request via h11 and stream the body as it arrives.

    Differences from :func:`forward_with_trailer`:

    * ``body`` yields upstream ciphertext chunks immediately, so the caller's
      client sees real time-to-first-token instead of one blob at the end.
    * ``trailers`` is a future that resolves with the ``EndOfMessage`` trailer
      headers only once the body is exhausted. Await it after streaming to
      settle payment from ``X-Tinfoil-Usage-Metrics``.
    * ``max_response_bytes`` is enforced incrementally, so an oversized
      response is aborted without first buffering it.

    Response headers are only available after the status line is parsed, which
    is why they are resolved before this coroutine returns.
    """
    reader, writer, conn, host = await _connect_and_send(
        method=method,
        url=url,
        headers=headers,
        body=body,
        timeout_seconds=timeout_seconds,
    )

    trailers_future: asyncio.Future[list[tuple[str, str]]] = (
        asyncio.get_running_loop().create_future()
    )
    released = False

    def _release() -> None:
        nonlocal released
        if released:
            return
        released = True
        _close_writer(writer)

    try:
        # Read up to and including the response headers before returning, so
        # callers can decide on status-code handling without consuming the body.
        status_code = 0
        resp_headers: list[tuple[str, str]] = []
        while True:
            event = await _read_event(
                conn, reader, host=host, timeout_seconds=timeout_seconds
            )
            if isinstance(event, h11.Response):
                status_code = event.status_code
                resp_headers = [(k.decode(), v.decode()) for k, v in event.headers]
                break
            if isinstance(event, h11.EndOfMessage):
                # No body at all (e.g. 204): settle trailers now.
                trailers_future.set_result(
                    [(k.decode(), v.decode()) for k, v in event.headers]
                )
                await _close_writer_and_wait(writer, close_timeout_seconds)

                async def _empty() -> AsyncIterator[bytes]:
                    return
                    yield b""  # pragma: no cover - makes this an async generator

                return StreamedTrailerResponse(
                    status_code=status_code,
                    headers=resp_headers,
                    body=_empty(),
                    trailers=trailers_future,
                )
            if isinstance(event, h11.ConnectionClosed):
                break

        async def _iter_body() -> AsyncIterator[bytes]:
            body_size = 0
            try:
                while True:
                    event = await _read_event(
                        conn, reader, host=host, timeout_seconds=timeout_seconds
                    )
                    if isinstance(event, h11.Data):
                        body_size += len(event.data)
                        if body_size > max_response_bytes:
                            raise ValueError(
                                f"EHBP response exceeded {max_response_bytes} bytes"
                            )
                        yield event.data
                    elif isinstance(event, h11.EndOfMessage):
                        pairs = [(k.decode(), v.decode()) for k, v in event.headers]
                        if not trailers_future.done():
                            trailers_future.set_result(pairs)
                        return
                    elif isinstance(event, h11.PAUSED):
                        logger.warning("h11 PAUSED event during EHBP response parsing")
                        break
                    elif isinstance(event, h11.ConnectionClosed):
                        break
            except GeneratorExit:
                # The consumer closed the stream (client disconnected).
                # Propagate the close but do not fault the trailers future: it
                # resolved to "no trailers observed", which is what settlement
                # must read. Marking it failed would surface a spurious error
                # from a read nobody is going to make.
                raise
            except BaseException as exc:  # noqa: BLE001 - must not leak
                if not trailers_future.done():
                    trailers_future.set_exception(exc)
                    # A caller that abandons the stream never awaits this
                    # future, and asyncio logs "exception was never retrieved"
                    # for that. Consume it here; awaiting callers still see the
                    # exception through their own await.
                    trailers_future.exception()
                raise
            finally:
                await _close_writer_and_wait(writer, close_timeout_seconds)
                if not trailers_future.done():
                    # Aborted before EndOfMessage: callers awaiting trailers
                    # must not hang.
                    trailers_future.set_result([])

        return StreamedTrailerResponse(
            status_code=status_code,
            headers=resp_headers,
            body=_iter_body(),
            trailers=trailers_future,
            _on_abandon=_release,
        )
    except BaseException:
        await _close_writer_and_wait(writer, close_timeout_seconds)
        raise
