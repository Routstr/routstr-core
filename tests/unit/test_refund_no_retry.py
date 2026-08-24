"""Refund token issuance must not repeat an ambiguous Cashu swap."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from routstr.upstream.base import BaseUpstreamProvider


@pytest.mark.asyncio
async def test_send_refund_does_not_retry_ambiguous_token_creation() -> None:
    provider = object.__new__(BaseUpstreamProvider)
    send_token = AsyncMock(side_effect=httpx.ReadTimeout("swap response lost"))
    store = AsyncMock()

    with (
        patch("routstr.upstream.base.send_token", send_token),
        patch("routstr.upstream.base.store_cashu_transaction", store),
        pytest.raises(HTTPException) as raised,
    ):
        await provider.send_refund(10, "sat", mint="https://mint.test")

    assert raised.value.status_code == 401
    send_token.assert_awaited_once_with(
        10, unit="sat", mint_url="https://mint.test"
    )
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_refund_does_not_retry_or_store_on_generic_failure() -> None:
    provider = object.__new__(BaseUpstreamProvider)
    send_token = AsyncMock(side_effect=Exception("mint rejected swap"))
    store = AsyncMock()

    with (
        patch("routstr.upstream.base.send_token", send_token),
        patch("routstr.upstream.base.store_cashu_transaction", store),
        pytest.raises(HTTPException) as raised,
    ):
        await provider.send_refund(10, "sat", mint="https://mint.test")

    assert raised.value.status_code == 401
    send_token.assert_awaited_once_with(
        10, unit="sat", mint_url="https://mint.test"
    )
    store.assert_not_awaited()
