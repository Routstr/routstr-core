from __future__ import annotations

import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock

import pytest

from routstr.core.error_scope import ERROR_SCOPE_UPSTREAM, UPSTREAM_ERROR_STATUS
from routstr.core.exceptions import EhbpConnectionError, EhbpTimeoutError, UpstreamError
from routstr.upstream.tinfoil_trailer import forward_with_trailer


class FakeReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class FakeWriter:
    def __init__(self) -> None:
        self.written = b""
        self.drain = AsyncMock()
        self.wait_closed = AsyncMock()
        self.close = MagicMock()

    def write(self, data: bytes) -> None:
        self.written += data


class HangingReader:
    """A reader that never returns data, used to trigger a read timeout."""

    async def read(self, _size: int) -> bytes:
        await asyncio.sleep(3600)
        return b""


@pytest.mark.asyncio
async def test_forward_with_trailer_captures_usage_trailer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Trailer: X-Tinfoil-Usage-Metrics\r\n"
        b"\r\n"
        b"5\r\nhello\r\n"
        b"0\r\n"
        b"X-Tinfoil-Usage-Metrics: prompt=1,completion=2,total=3\r\n"
        b"\r\n"
    )
    reader = FakeReader([response])
    writer = FakeWriter()
    open_connection = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(
        "routstr.upstream.tinfoil_trailer.asyncio.open_connection", open_connection
    )

    result = await forward_with_trailer(
        method="POST",
        url="https://enclave.tinfoil.sh/v1/chat/completions?stream=true",
        headers={"Authorization": "Bearer upstream"},
        body=b"opaque",
    )

    assert result.status_code == 200
    assert result.body == b"hello"
    assert result.trailers == [
        ("x-tinfoil-usage-metrics", "prompt=1,completion=2,total=3")
    ]
    assert b"Connection: close" in writer.written
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_forward_with_trailer_strips_hop_by_hop_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
    reader = FakeReader([response])
    writer = FakeWriter()
    monkeypatch.setattr(
        "routstr.upstream.tinfoil_trailer.asyncio.open_connection",
        AsyncMock(return_value=(reader, writer)),
    )

    await forward_with_trailer(
        method="POST",
        url="https://enclave.tinfoil.sh/v1/chat/completions",
        headers={
            "Authorization": "Bearer upstream",
            "Connection": "keep-alive, X-Client-Hop",
            "Keep-Alive": "timeout=5",
            "Proxy-Authenticate": "Basic",
            "Proxy-Authorization": "Basic secret",
            "TE": "trailers",
            "Trailer": "X-Usage",
            "Transfer-Encoding": "chunked",
            "Upgrade": "websocket",
            "X-Client-Hop": "remove-me",
            "X-End-To-End": "preserve-me",
        },
        body=b"opaque",
    )

    serialized_headers = writer.written.split(b"\r\n\r\n", 1)[0].lower()
    for name in (
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te:",
        b"trailer:",
        b"transfer-encoding",
        b"upgrade:",
        b"x-client-hop",
    ):
        assert name not in serialized_headers
    assert b"connection: close" in serialized_headers
    assert b"content-length: 6" in serialized_headers
    assert b"x-end-to-end: preserve-me" in serialized_headers


@pytest.mark.asyncio
async def test_forward_with_trailer_enforces_response_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
    reader = FakeReader([response])
    writer = FakeWriter()
    monkeypatch.setattr(
        "routstr.upstream.tinfoil_trailer.asyncio.open_connection",
        AsyncMock(return_value=(reader, writer)),
    )

    with pytest.raises(ValueError, match="EHBP response exceeded"):
        await forward_with_trailer(
            method="POST",
            url="https://enclave.tinfoil.sh/v1/chat/completions",
            headers={},
            body=b"opaque",
            max_response_bytes=4,
        )

    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_forward_with_trailer_connect_timeout_raises_ehbp_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _hang_connect(*_args: object, **_kwargs: object) -> object:
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        "routstr.upstream.tinfoil_trailer.asyncio.open_connection", _hang_connect
    )

    with pytest.raises(EhbpTimeoutError, match="connecting"):
        await forward_with_trailer(
            method="POST",
            url="https://enclave.tinfoil.sh/v1/chat/completions",
            headers={},
            body=b"opaque",
        )


@pytest.mark.asyncio
async def test_forward_with_trailer_tls_handshake_timeout_raises_ehbp_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stdlib TLS handshake timer surfaces as ConnectionAbortedError.

    CPython aborts a slow handshake with ``ConnectionAbortedError`` rather
    than ``asyncio.TimeoutError``, so the connect handler must classify it as
    an upstream timeout — otherwise it escapes to the node-scoped 500 in
    ``forward_ehbp_request``.
    """

    async def _handshake_timeout(*_args: object, **_kwargs: object) -> object:
        raise ConnectionAbortedError(
            "SSL handshake is taking longer than 60.0 seconds: aborting the connection"
        )

    monkeypatch.setattr(
        "routstr.upstream.tinfoil_trailer.asyncio.open_connection",
        _handshake_timeout,
    )

    with pytest.raises(EhbpTimeoutError, match="TLS handshake timed out"):
        await forward_with_trailer(
            method="POST",
            url="https://inference.tinfoil.sh/v1/chat/completions",
            headers={},
            body=b"opaque",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("connection refused"),
        ConnectionResetError("connection reset"),
        ssl.SSLError("certificate verify failed"),
        OSError("name resolution failed"),
    ],
)
async def test_forward_with_trailer_connection_failure_raises_ehbp_connection(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """Non-timeout connect failures must be upstream-scoped, not node 500s."""

    async def _fail_connect(*_args: object, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(
        "routstr.upstream.tinfoil_trailer.asyncio.open_connection", _fail_connect
    )

    with pytest.raises(EhbpConnectionError, match="Unable to connect"):
        await forward_with_trailer(
            method="POST",
            url="https://inference.tinfoil.sh/v1/chat/completions",
            headers={},
            body=b"opaque",
        )


@pytest.mark.asyncio
async def test_forward_with_trailer_read_timeout_raises_ehbp_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = HangingReader()
    writer = FakeWriter()
    monkeypatch.setattr(
        "routstr.upstream.tinfoil_trailer.asyncio.open_connection",
        AsyncMock(return_value=(reader, writer)),
    )

    with pytest.raises(EhbpTimeoutError, match="waiting for response data"):
        await forward_with_trailer(
            method="POST",
            url="https://enclave.tinfoil.sh/v1/chat/completions",
            headers={},
            body=b"opaque",
            timeout_seconds=0.01,
        )

    writer.close.assert_called_once()


def test_ehbp_timeout_error_metadata() -> None:
    exc = EhbpTimeoutError("boom")
    assert exc.status_code == UPSTREAM_ERROR_STATUS
    assert exc.code == "UPSTREAM_TIMEOUT"
    assert exc.details is None
    assert exc.scope == ERROR_SCOPE_UPSTREAM
    assert isinstance(exc, UpstreamError)


def test_ehbp_timeout_error_forwards_details() -> None:
    """``details`` must survive so the response builder can forward it."""
    exc = EhbpTimeoutError("boom", details={"phase": "connect"})
    assert exc.details == {"phase": "connect"}
    assert exc.status_code == UPSTREAM_ERROR_STATUS
    assert exc.code == "UPSTREAM_TIMEOUT"


def test_ehbp_connection_error_metadata() -> None:
    exc = EhbpConnectionError("boom")
    assert exc.status_code == UPSTREAM_ERROR_STATUS
    assert exc.code == "UPSTREAM_UNAVAILABLE"
    assert exc.details is None
    assert exc.scope == ERROR_SCOPE_UPSTREAM
    assert isinstance(exc, UpstreamError)


def test_ehbp_connection_error_forwards_details() -> None:
    exc = EhbpConnectionError("boom", details={"provider": "tinfoil"})
    assert exc.details == {"provider": "tinfoil"}
    assert exc.code == "UPSTREAM_UNAVAILABLE"
