import asyncio

import httpx
import pytest

from routstr.upstream.http_client import (
    close_upstream_http_client,
    get_upstream_http_client,
)


@pytest.mark.asyncio
async def test_upstream_http_client_is_reused_until_shutdown() -> None:
    first = get_upstream_http_client()
    second = get_upstream_http_client()

    assert second is first
    assert not first.is_closed

    await close_upstream_http_client()
    assert first.is_closed

    replacement = get_upstream_http_client()
    try:
        assert replacement is not first
        assert not replacement.is_closed
    finally:
        await close_upstream_http_client()


@pytest.mark.asyncio
async def test_upstream_http_client_has_bounded_pool_wait_and_read_timeout() -> None:
    client = get_upstream_http_client()
    try:
        assert client.timeout.pool == 5.0
        assert client.timeout.read == 300.0
        assert getattr(client._transport, "_pool")._max_connections == 100
    finally:
        await close_upstream_http_client()


@pytest.mark.asyncio
async def test_upstream_http_client_does_not_share_cookies() -> None:
    client = get_upstream_http_client()
    try:
        first = client.build_request("GET", "https://example.com/test")
        response = httpx.Response(
            200,
            headers={"set-cookie": "sticky=upstream; Path=/"},
            request=first,
        )
        client.cookies.extract_cookies(response)

        later = client.build_request("GET", "https://example.com/test")
        explicit = client.build_request(
            "GET", "https://example.com/test", headers={"cookie": "user=provided"}
        )

        assert "cookie" not in later.headers
        assert explicit.headers["cookie"] == "user=provided"
    finally:
        await close_upstream_http_client()


@pytest.mark.asyncio
async def test_upstream_http_client_cannot_reopen_during_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = get_upstream_http_client()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    original_close = client.aclose

    async def delayed_close() -> None:
        close_started.set()
        await allow_close.wait()
        await original_close()

    monkeypatch.setattr(client, "aclose", delayed_close)
    closing = asyncio.create_task(close_upstream_http_client())
    await close_started.wait()

    with pytest.raises(RuntimeError, match="shutting down"):
        get_upstream_http_client()

    allow_close.set()
    await closing
