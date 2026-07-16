"""h11-based HTTP client for EHBP requests that captures HTTP trailers.

httpx/httpcore silently discard HTTP trailers during chunked transfer
decoding. Tinfoil returns ``X-Tinfoil-Usage-Metrics`` as a trailer on
streaming responses, so we need a lower-level HTTP client that preserves
trailers from the h11 ``EndOfMessage`` event.

Because EHBP response bodies are opaque encrypted blobs, buffering the full
response is acceptable — the client decrypts the complete body regardless of
whether it arrived streamed or buffered.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import h11

from ..core import get_logger

logger = get_logger(__name__)

_READ_BUFSIZE = 65536
_DEFAULT_TIMEOUT_SECONDS = 30.0
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
    trailer headers from the ``EndOfMessage`` event.
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
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ssl_ctx),
        timeout=timeout_seconds,
    )

    try:
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
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)

        # Parse response with h11
        conn = h11.Connection(h11.CLIENT)
        status_code = 0
        resp_headers: list[tuple[str, str]] = []
        body_chunks: list[bytes] = []
        body_size = 0
        trailers: list[tuple[str, str]] = []

        while True:
            event = conn.next_event()

            if event is h11.NEED_DATA:
                data = await asyncio.wait_for(
                    reader.read(_READ_BUFSIZE),
                    timeout=timeout_seconds,
                )
                conn.receive_data(data if data else b"")
                continue

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
        writer.close()
        if close_timeout_seconds > 0:
            try:
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=close_timeout_seconds
                )
            except Exception:
                pass
