"""Compatibility shims that keep cashu 0.21.x working with routstr's deps and mints.

cashu's ``async_set_httpx_client`` decorator builds the client for *every* mint
call as ``httpx.AsyncClient(proxies=proxies_dict, ...)``. httpx deprecated
``proxies`` in 0.26 and removed it in 0.28, so on httpx>=0.28 every wallet
operation routstr performs -- ``load_mint_keysets``, ``mint_quote``,
``melt_quote``, token redeem -- raises::

    TypeError: AsyncClient.__init__() got an unexpected keyword argument 'proxies'

Upstream cashu (0.21.0, the latest release) still caps ``httpx<0.26`` and has no
release that fixes this, while litellm>=1.84 requires ``httpx>=0.28``. We bridge
the gap by translating the keyword *inside cashu's module namespace only* --
global httpx behaviour is untouched, and the shim is a no-op on httpx<0.28.

Delete this module once cashu ships a release that passes ``proxy=``.
"""

from typing import Any

import httpx

__all__ = ["install_cashu_httpx_shim", "install_cashu_mint_compat"]

_ALL_SCHEMES = "all://"


def _single_proxy(proxies: Any) -> str | None:
    """Collapse an httpx<0.28 ``proxies`` mapping into a single ``proxy`` URL.

    cashu only ever builds ``{}`` or ``{"all://": url}``, so a mapping with one
    distinct URL is all we need to support. Anything richer is unrepresentable
    as httpx 0.28's scalar ``proxy=``; we fail closed and raise rather than
    return ``None``, because dropping the entry would silently send mint
    traffic direct instead of through the configured Tor/SOCKS proxy.
    """
    if not proxies:
        return None
    if isinstance(proxies, str):
        return proxies
    if isinstance(proxies, dict):
        if _ALL_SCHEMES in proxies:
            value = proxies[_ALL_SCHEMES]
            return str(value) if value is not None else None
        distinct = {str(v) for v in proxies.values() if v is not None}
        if not distinct:
            return None
        if len(distinct) == 1:
            return distinct.pop()
    raise ValueError(
        f"cannot represent proxies={proxies!r} as httpx 0.28 proxy=; "
        "refusing to send proxied traffic direct"
    )


class _ProxiesCompatAsyncClient(httpx.AsyncClient):
    """``httpx.AsyncClient`` that still accepts the removed ``proxies`` kwarg."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if "proxies" in kwargs:
            proxies = kwargs.pop("proxies")
            proxy = _single_proxy(proxies)
            if proxy is not None and kwargs.get("proxy") is None:
                kwargs["proxy"] = proxy
            elif isinstance(proxies, dict) and not proxies:
                # In httpx<0.28, proxies={} disabled environment proxy discovery.
                kwargs.setdefault("trust_env", False)
        super().__init__(*args, **kwargs)


class _HttpxNamespace:
    """Stand-in for the ``httpx`` module inside cashu's ``v1_api``.

    Every attribute resolves against the real module except ``AsyncClient``,
    so cashu keeps using genuine httpx types everywhere else.
    """

    AsyncClient = _ProxiesCompatAsyncClient

    def __getattr__(self, name: str) -> Any:
        return getattr(httpx, name)


def _httpx_accepts_proxies() -> bool:
    import inspect

    try:
        return "proxies" in inspect.signature(httpx.AsyncClient.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False


def install_cashu_httpx_shim() -> bool:
    """Patch cashu's mint client to survive httpx>=0.28.

    Returns True when the shim was installed, False when it wasn't needed.
    Safe to call repeatedly.
    """
    if _httpx_accepts_proxies():
        return False

    from cashu.wallet import v1_api

    if isinstance(getattr(v1_api, "httpx", None), _HttpxNamespace):
        return True

    v1_api.httpx = _HttpxNamespace()  # type: ignore[assignment]
    return True


_NUT20_SIGNATURE_INVALID = "(Code: 20008)"


def install_cashu_mint_compat() -> None:
    """Keep cashu 0.21 wallets working against pre-0.21 mints.

    Older mints (cdk-mintd 0.17, Nutshell <0.20.2) differ from cashu 0.21 in
    two ways, each of which breaks minting or melting outright:

    - They omit the NUT-04/05 ``method`` field cashu 0.21 made required on
      quote responses. CDK defaults it to ``bolt11``; so do we, as cashu only
      requests quotes on its bolt11 endpoints.
    - They verify NUT-20 mint signatures only in the legacy message format,
      while cashu 0.21 signs only in the new one. A rejected mint issues
      nothing, so on that rejection we retry once with a legacy signature.

    Safe to call repeatedly.
    """
    from cashu.core.models import PostMeltQuoteResponse, PostMintQuoteResponse
    from cashu.core.nuts import nut20
    from cashu.wallet import v1_api
    from cashu.wallet.crud import get_bolt11_mint_quote

    for model in (PostMintQuoteResponse, PostMeltQuoteResponse):
        field = model.model_fields.get("method")
        if field is not None and field.is_required():
            field.default = "bolt11"
            model.model_rebuild(force=True)

    mint = v1_api.LedgerAPI.mint
    if getattr(mint, "_routstr_legacy_nut20", False):
        return

    async def mint_with_legacy_nut20_fallback(
        self: Any, outputs: list, quote: str, signature: str | None = None
    ) -> Any:
        try:
            return await mint(self, outputs, quote, signature)
        except Exception as e:
            if signature is None or _NUT20_SIGNATURE_INVALID not in str(e):
                raise
            local = await get_bolt11_mint_quote(db=self.db, quote=quote)
            if local is None or not local.privkey:
                raise
            message = nut20.construct_message_legacy(quote, outputs)
            privkey = nut20.PrivateKey(bytes.fromhex(local.privkey))
            legacy = privkey.sign_schnorr(message).hex()
            return await mint(self, outputs, quote, legacy)

    mint_with_legacy_nut20_fallback._routstr_legacy_nut20 = True  # type: ignore[attr-defined]
    v1_api.LedgerAPI.mint = mint_with_legacy_nut20_fallback  # type: ignore[method-assign]
