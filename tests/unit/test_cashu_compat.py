"""Pins routstr.cashu_compat: the httpx shim for cashu 0.21's removed `proxies`
kwarg, and the mint compat for older mints (missing `method`, legacy NUT-20)."""

import asyncio
from types import SimpleNamespace

import httpcore
import httpx
import pytest
from cashu.core.base import BlindedMessage
from cashu.core.models import PostMeltQuoteResponse, PostMintQuoteResponse
from cashu.core.nuts import nut20
from cashu.wallet import crud, v1_api
from coincurve import PublicKeyXOnly
from httpx import AsyncClient

from routstr.cashu_compat import (
    _ProxiesCompatAsyncClient,
    _single_proxy,
    install_cashu_httpx_shim,
    install_cashu_mint_compat,
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


async def test_cashu_decorator_routes_through_configured_socks_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_cashu_httpx_shim()
    monkeypatch.setattr(v1_api.settings, "tor", False)
    monkeypatch.setattr(v1_api.settings, "socks_proxy", "localhost:9050")

    class _Ledger:
        url = "http://mint.test/"
        httpx: AsyncClient

        @v1_api.async_set_httpx_client  # type: ignore[misc]
        async def call(self) -> AsyncClient:
            return self.httpx

    client: httpx.AsyncClient = await _Ledger().call()
    try:
        transports = list(client._mounts.values())
        assert len(transports) == 1
        pool = transports[0]._pool  # type: ignore[union-attr]
        assert isinstance(pool, httpcore.AsyncSOCKSProxy)
        assert pool._proxy_url == httpcore.URL("socks5://localhost:9050")
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


def test_quote_responses_from_pre_method_mints_default_to_bolt11() -> None:
    """cdk-mintd 0.17 and Nutshell 0.20.0 omit `method` from quote responses."""
    install_cashu_mint_compat()
    install_cashu_mint_compat()
    common = {"quote": "q", "request": "lnbc1", "amount": 21, "unit": "sat"}
    common.update(state="UNPAID", expiry=1)
    melt = PostMeltQuoteResponse.model_validate({**common, "fee_reserve": 2})
    mint = PostMintQuoteResponse.model_validate(common)
    assert melt.method == mint.method == "bolt11"
    sent = PostMeltQuoteResponse.model_validate(
        {**common, "fee_reserve": 2, "method": "bolt12"}
    )
    assert sent.method == "bolt12"


def test_mint_compat_install_does_not_double_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def plain_mint(
        self: object, outputs: list, quote: str, signature: str | None = None
    ) -> list:
        return []

    monkeypatch.setattr(v1_api.LedgerAPI, "mint", plain_mint)
    install_cashu_mint_compat()
    wrapped = v1_api.LedgerAPI.mint
    assert wrapped is not plain_mint
    install_cashu_mint_compat()
    assert v1_api.LedgerAPI.mint is wrapped


async def test_mint_retries_with_legacy_nut20_signature_on_old_mints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cdk-mintd 0.17 and Nutshell <0.20.2 only verify legacy NUT-20 signatures."""
    privkey, pubkey = nut20.generate_keypair()
    outputs = [BlindedMessage(id="00", amount=1, B_=nut20.generate_keypair()[1])]
    seen: list[str | None] = []

    async def old_mint(
        self: object, outputs: list, quote: str, signature: str | None = None
    ) -> list:
        seen.append(signature)
        legacy = nut20.construct_message_legacy(quote, outputs)
        key = PublicKeyXOnly(bytes.fromhex(pubkey)[1:])
        if signature and key.verify(bytes.fromhex(signature), legacy):
            return ["promise"]
        raise Exception("Mint Error: Signature for mint request invalid (Code: 20008)")

    async def local_quote(db: object, quote: str) -> SimpleNamespace:
        return SimpleNamespace(privkey=privkey)

    monkeypatch.setattr(v1_api.LedgerAPI, "mint", old_mint)
    monkeypatch.setattr(crud, "get_bolt11_mint_quote", local_quote)
    install_cashu_mint_compat()
    install_cashu_mint_compat()

    wallet = SimpleNamespace(db=None)
    current = nut20.sign_mint_quote("q", outputs, privkey)
    assert await v1_api.LedgerAPI.mint(wallet, outputs, "q", current) == ["promise"]
    assert len(seen) == 2 and seen[0] == current and seen[1] != current

    seen.clear()
    with pytest.raises(Exception, match="20008"):
        await v1_api.LedgerAPI.mint(wallet, outputs, "q", None)
    assert seen == [None]


@pytest.mark.parametrize(
    "error, local, expected_calls",
    [
        ("Mint Error: quote not paid (Code: 20001)", "privkey", 1),
        ("Mint Error: Signature invalid (Code: 20008)", None, 1),
        ("Mint Error: Signature invalid (Code: 20008)", "", 1),
        ("Mint Error: Signature invalid (Code: 20008)", "privkey", 2),
    ],
    ids=["other-error", "no-local-quote", "empty-privkey", "legacy-rejected"],
)
async def test_mint_legacy_nut20_fallback_reraises(
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    local: str | None,
    expected_calls: int,
) -> None:
    privkey, _ = nut20.generate_keypair()
    outputs = [BlindedMessage(id="00", amount=1, B_=nut20.generate_keypair()[1])]
    seen: list[str | None] = []

    async def rejecting_mint(
        self: object, outputs: list, quote: str, signature: str | None = None
    ) -> list:
        seen.append(signature)
        raise Exception(error)

    async def local_quote(db: object, quote: str) -> SimpleNamespace | None:
        if local is None:
            return None
        return SimpleNamespace(privkey=privkey if local == "privkey" else local)

    monkeypatch.setattr(v1_api.LedgerAPI, "mint", rejecting_mint)
    monkeypatch.setattr(crud, "get_bolt11_mint_quote", local_quote)
    install_cashu_mint_compat()

    current = nut20.sign_mint_quote("q", outputs, privkey)
    with pytest.raises(Exception) as excinfo:
        await v1_api.LedgerAPI.mint(SimpleNamespace(db=None), outputs, "q", current)
    assert str(excinfo.value) == error
    assert len(seen) == expected_calls and seen[0] == current
