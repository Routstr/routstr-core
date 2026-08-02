import asyncio
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
async def test_expired_cooldown_allows_probe_in_wallet_mutation_scope() -> None:
    guard = MintRateGuard("http://mint:3338", max_concurrency=1)
    guard.apply_cooldown(0, reason="rate_limited")
    operation = AsyncMock(return_value="recovered")

    async with fail_fast_mint_operations():
        result = await guard.run(operation)

    assert result == "recovered"
    operation.assert_awaited_once()
    assert guard._needs_probe is False


@pytest.mark.asyncio
async def test_fail_fast_does_not_wait_behind_existing_probe() -> None:
    guard = MintRateGuard("http://mint:3338", max_concurrency=1)
    guard.apply_cooldown(0, reason="rate_limited")
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def probe() -> str:
        probe_started.set()
        await release_probe.wait()
        return "recovered"

    first = asyncio.create_task(guard.run(probe))
    await probe_started.wait()
    try:
        async with fail_fast_mint_operations():
            with pytest.raises(MintCooldownError):
                await asyncio.wait_for(guard.run(AsyncMock()), timeout=0.05)
    finally:
        release_probe.set()
        assert await first == "recovered"


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


async def test_guard_concurrency_change_preserves_cooldown_state() -> None:
    from routstr.core.settings import settings

    mint_url = "https://mint.test-concurrency-carryover"
    with patch.object(settings, "mint_max_concurrency", 2):
        guard = MintRateGuard.get(mint_url)
        guard.apply_cooldown(120.0, reason="rate_limited")
        guard._consecutive_rate_limits = 3

    with patch.object(settings, "mint_max_concurrency", 5):
        rebuilt = MintRateGuard.get(mint_url)

    assert rebuilt is not guard
    assert rebuilt.cooldown_remaining() > 0
    assert rebuilt._cooldown_reason == "rate_limited"
    assert rebuilt._consecutive_rate_limits == 3
