"""Compatibility shim that keeps cashu 0.20.x working on httpx>=0.28.

cashu's ``async_set_httpx_client`` decorator builds the client for *every* mint
call as ``httpx.AsyncClient(proxies=proxies_dict, ...)``. httpx deprecated
``proxies`` in 0.26 and removed it in 0.28, so on httpx>=0.28 every wallet
operation routstr performs -- ``load_mint_keysets``, ``mint_quote``,
``melt_quote``, token redeem -- raises::

    TypeError: AsyncClient.__init__() got an unexpected keyword argument 'proxies'

Upstream cashu (0.20.3, the latest release) still caps ``httpx<0.26`` and has no
release that fixes this, while litellm>=1.84 requires ``httpx>=0.28``. We bridge
the gap by translating the keyword *inside cashu's module namespace only* --
global httpx behaviour is untouched, and the shim is a no-op on httpx<0.28.

Delete this module once cashu ships a release that passes ``proxy=``.
"""

from typing import Any

import httpx

__all__ = ["install_cashu_httpx_shim"]

_ALL_SCHEMES = "all://"


def _single_proxy(proxies: Any) -> str | None:
    """Collapse an httpx<0.28 ``proxies`` mapping into a single ``proxy`` URL.

    cashu only ever builds ``{}`` or ``{"all://": url}``, so a mapping with one
    distinct URL is all we need to support; anything richer is unrepresentable
    as httpx 0.28's scalar ``proxy=`` and is dropped rather than guessed at.
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
        if len(distinct) == 1:
            return distinct.pop()
    return None


class _ProxiesCompatAsyncClient(httpx.AsyncClient):
    """``httpx.AsyncClient`` that still accepts the removed ``proxies`` kwarg."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if "proxies" in kwargs:
            proxies = kwargs.pop("proxies")
            proxy = _single_proxy(proxies)
            if proxy is not None and kwargs.get("proxy") is None:
                kwargs["proxy"] = proxy
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
