"""cashu 0.20.x builds its mint client with the `proxies` kwarg httpx removed in
0.28. These tests pin the shim that keeps every wallet call working."""

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
        ({"http://": "http://a:1", "https://": "http://b:2"}, None),
    ],
)
def test_single_proxy_collapses_cashu_mappings(
    proxies: object, expected: str | None
) -> None:
    assert _single_proxy(proxies) == expected


@pytest.mark.parametrize("proxies", [{}, {"all://": "socks5://localhost:9050"}])
async def test_compat_client_accepts_proxies(proxies: dict) -> None:
    async with _ProxiesCompatAsyncClient(
        proxies=proxies, base_url="http://mint.test"
    ) as client:
        assert isinstance(client, httpx.AsyncClient)


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
    assert install_cashu_httpx_shim() is True
    patched = v1_api.httpx
    assert install_cashu_httpx_shim() is True
    assert v1_api.httpx is patched


def test_shim_namespace_passes_through_other_httpx_attributes() -> None:
    install_cashu_httpx_shim()
    assert v1_api.httpx.Response is httpx.Response
    assert v1_api.httpx.AsyncClient is _ProxiesCompatAsyncClient
