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
