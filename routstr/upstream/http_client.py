"""Lifecycle-managed HTTP client for latency-sensitive upstream inference calls."""

import asyncio

import httpx

UPSTREAM_MAX_CONNECTIONS = 100
UPSTREAM_MAX_KEEPALIVE_CONNECTIONS = 20
UPSTREAM_POOL_TIMEOUT_SECONDS = 5.0
UPSTREAM_READ_TIMEOUT_SECONDS = 300.0

_upstream_http_client: httpx.AsyncClient | None = None
_upstream_http_client_loop: asyncio.AbstractEventLoop | None = None
_upstream_http_client_closing = False


class _StatelessCookies(httpx.Cookies):
    """Preserve caller Cookie headers without sharing upstream cookies."""

    def extract_cookies(self, response: httpx.Response) -> None:
        return

    def set_cookie_header(self, request: httpx.Request) -> None:
        return


def get_upstream_http_client() -> httpx.AsyncClient:
    """Return the process-wide client so upstream origins reuse pooled connections."""
    global _upstream_http_client, _upstream_http_client_loop
    if _upstream_http_client_closing:
        raise RuntimeError("Upstream HTTP client is shutting down")

    loop = asyncio.get_running_loop()
    if (
        _upstream_http_client is None
        or _upstream_http_client.is_closed
        or _upstream_http_client_loop is not loop
    ):
        previous = _upstream_http_client
        client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(
                limits=httpx.Limits(
                    max_connections=UPSTREAM_MAX_CONNECTIONS,
                    max_keepalive_connections=UPSTREAM_MAX_KEEPALIVE_CONNECTIONS,
                ),
            ),
            timeout=httpx.Timeout(
                connect=30.0,
                read=UPSTREAM_READ_TIMEOUT_SECONDS,
                write=30.0,
                pool=UPSTREAM_POOL_TIMEOUT_SECONDS,
            ),
        )
        # AsyncClient's public setter copies into a concrete Cookies jar.
        # Replace the backing jar so response cookies are never retained.
        client._cookies = _StatelessCookies()
        _upstream_http_client = client
        _upstream_http_client_loop = loop
        if previous is not None and not previous.is_closed:
            loop.create_task(previous.aclose())
    return _upstream_http_client


async def close_upstream_http_client() -> None:
    """Close the shared connection pool during application shutdown."""
    global _upstream_http_client, _upstream_http_client_loop
    global _upstream_http_client_closing

    client = _upstream_http_client
    if client is None:
        return

    _upstream_http_client_closing = True
    try:
        if not client.is_closed:
            await client.aclose()
    finally:
        if _upstream_http_client is client:
            _upstream_http_client = None
            _upstream_http_client_loop = None
        _upstream_http_client_closing = False
