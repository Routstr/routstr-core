"""Lifecycle-managed HTTP client for latency-sensitive upstream inference calls."""

import asyncio

import httpx

_upstream_http_client: httpx.AsyncClient | None = None
_upstream_http_client_loop: asyncio.AbstractEventLoop | None = None


def get_upstream_http_client() -> httpx.AsyncClient:
    """Return the process-wide client so upstream origins reuse pooled connections."""
    global _upstream_http_client, _upstream_http_client_loop
    loop = asyncio.get_running_loop()
    if (
        _upstream_http_client is None
        or _upstream_http_client.is_closed
        or _upstream_http_client_loop is not loop
    ):
        previous = _upstream_http_client
        _upstream_http_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=1),
            timeout=None,
        )
        _upstream_http_client_loop = loop
        if previous is not None and not previous.is_closed:
            loop.create_task(previous.aclose())
    return _upstream_http_client


async def close_upstream_http_client() -> None:
    """Close the shared connection pool during application shutdown."""
    global _upstream_http_client, _upstream_http_client_loop
    client = _upstream_http_client
    _upstream_http_client = None
    _upstream_http_client_loop = None
    if client is not None and not client.is_closed:
        await client.aclose()
