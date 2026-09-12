"""cashu 0.20.x builds its mint client with the `proxies` kwarg httpx removed in
0.28. These tests pin the shim that keeps every wallet call working."""

import asyncio

import httpx
import pytest
from cashu.wallet import v1_api
from httpx import AsyncClient

from routstr.cashu_compat import (
    _ProxiesCompatAsyncClient,
    _single_proxy,
    install_cashu_httpx_shim,
)


def test_httpx_no_longer_accepts_proxies() -> None:
    """The premise of the shim: plain httpx rejects what cashu passes."""
    with pytest.raises(TypeError):
        httpx.AsyncClient(proxies={})  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "proxies, expected",
    [
        ({}, None),
        (None, None),
        ({"all://": "socks5://localhost:9050"}, "socks5://localhost:9050"),
        ("socks5://localhost:9050", "socks5://localhost:9050"),
        ({"http://": "http://p:1", "https://": "http://p:1"}, "http://p:1"),
    ],
)
def test_single_proxy_collapses_cashu_mappings(
    proxies: object, expected: str | None
) -> None:
    assert _single_proxy(proxies) == expected


def test_single_proxy_fails_closed_on_unrepresentable_mapping() -> None:
    """Never silently drop a proxy: that would send mint traffic direct."""
    with pytest.raises(ValueError, match="cannot represent proxies"):
        _single_proxy({"http://": "http://a:1", "https://": "http://b:2"})


@pytest.mark.parametrize("proxies", [{}, {"all://": "socks5://localhost:9050"}])
async def test_compat_client_accepts_proxies(proxies: dict) -> None:
    async with _ProxiesCompatAsyncClient(
        proxies=proxies, base_url="http://mint.test"
    ) as client:
        assert isinstance(client, httpx.AsyncClient)


async def test_empty_proxy_map_disables_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_url = "http://127.0.0.1:1"
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    async def respond(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(0)
        writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/"

        async with _ProxiesCompatAsyncClient(proxies={}, timeout=1) as client:
            assert (await client.get(url)).status_code == 204

        async with _ProxiesCompatAsyncClient(
            proxies={}, trust_env=True, timeout=1
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(url)
    finally:
        server.close()
        await server.wait_closed()


async def test_cashu_decorator_builds_a_client_after_shim() -> None:
    """The real cashu decorator — the code path every mint call goes through."""
    install_cashu_httpx_shim()

    class _Ledger:
        url = "http://mint.test/"
        # cashu's decorator assigns the client here; alias avoids shadowing.
        httpx: AsyncClient

        @v1_api.async_set_httpx_client  # type: ignore[misc]
        async def call(self) -> AsyncClient:
            return self.httpx

    client: httpx.AsyncClient = await _Ledger().call()
    try:
        assert isinstance(client, httpx.AsyncClient)
        assert str(client.base_url) == "http://mint.test"
    finally:
        await client.aclose()


def test_install_is_idempotent() -> None:
    install_cashu_httpx_shim()
    patched = v1_api.httpx
    install_cashu_httpx_shim()
    assert v1_api.httpx is patched


def test_shim_namespace_passes_through_other_httpx_attributes() -> None:
    install_cashu_httpx_shim()
    assert v1_api.httpx.Response is httpx.Response
    assert v1_api.httpx.AsyncClient is _ProxiesCompatAsyncClient
