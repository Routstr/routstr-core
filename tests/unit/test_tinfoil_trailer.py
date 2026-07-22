from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

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
