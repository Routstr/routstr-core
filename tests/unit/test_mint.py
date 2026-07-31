from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from cashu.core.base import Unit

from routstr.mint import (
    MintCooldownError,
    MintRateGuard,
    MintRateLimitedError,
    fail_fast_mint_operations,
)
from routstr.wallet import Wallet


@pytest.mark.asyncio
async def test_cooldown_fails_fast_while_wallet_mutation_scope_is_held() -> None:
    guard = MintRateGuard("http://mint:3338", max_concurrency=1)
    guard.apply_cooldown(3600, reason="rate_limited")
    operation = AsyncMock(return_value="should not run")

    with (
        patch("routstr.mint.asyncio.sleep", AsyncMock()) as sleep,
        pytest.raises(MintCooldownError) as caught,
    ):
        async with fail_fast_mint_operations():
            await guard.run(operation)

    assert caught.value.retry_after_seconds > 0
    operation.assert_not_awaited()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_cashu_429_dispatches_through_wallet_override() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            request=request,
            json={"detail": "too many requests", "code": 42900},
        )

    wallet = object.__new__(Wallet)
    wallet.url = "http://mint:3338"
    wallet.db = Mock()
    wallet.keysets = {"loaded": Mock()}
    wallet.mint_info = Mock()
    wallet.mint_info.requires_blind_auth_path.return_value = False
    wallet.mint_info.requires_clear_auth_path.return_value = False
    wallet.auth_db = None
    wallet.auth_keyset_id = None

    real_client = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_client(
            transport=httpx.MockTransport(handler),
            base_url=str(kwargs["base_url"]),
        )

    with (
        patch("cashu.wallet.v1_api.httpx.AsyncClient", side_effect=client_factory),
        pytest.raises(MintRateLimitedError),
    ):
        await wallet.mint_quote(1, Unit.sat)
