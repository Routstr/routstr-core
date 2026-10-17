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
    passive_mint_reads,
    run_mint_operation,
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


@pytest.mark.asyncio
async def test_wrapped_transport_failure_opens_central_cooldown() -> None:
    mint_url = "https://transport-failure.test"
    MintRateGuard._guards.pop(mint_url, None)

    async def wrapped_failure() -> None:
        try:
            raise httpx.ReadTimeout("body stalled")
        except httpx.ReadTimeout as error:
            raise Exception("wallet wrapper") from error

    with pytest.raises(Exception, match="wallet wrapper"):
        await run_mint_operation(
            wrapped_failure,
            mint_url=mint_url,
            retry_timeouts=False,
        )

    guard = MintRateGuard.get(mint_url)
    assert guard.cooldown_remaining() > 29
    probe = AsyncMock()
    async with fail_fast_mint_operations():
        with pytest.raises(MintCooldownError):
            await guard.run(probe)
    probe.assert_not_awaited()
    MintRateGuard._guards.pop(mint_url, None)


@pytest.mark.asyncio
async def test_timeout_retry_succeeds_without_opening_cooldown() -> None:
    from routstr.core.settings import settings

    mint_url = "https://retryable-timeout.test"
    MintRateGuard._guards.pop(mint_url, None)
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("first attempt stalled")
        return "ok"

    with (
        patch.object(settings, "mint_retry_max_attempts", 2),
        patch("routstr.mint.asyncio.sleep", AsyncMock()),
    ):
        result = await run_mint_operation(flaky, mint_url=mint_url)

    assert result == "ok"
    assert calls == 2
    assert MintRateGuard.get(mint_url).cooldown_remaining() == 0.0
    MintRateGuard._guards.pop(mint_url, None)


@pytest.mark.asyncio
async def test_exhausted_timeout_retries_open_transport_cooldown() -> None:
    from routstr.core.settings import settings

    mint_url = "https://exhausted-timeout.test"
    MintRateGuard._guards.pop(mint_url, None)

    async def always_timeout() -> None:
        raise httpx.ReadTimeout("stalled")

    with (
        patch.object(settings, "mint_retry_max_attempts", 1),
        patch("routstr.mint.asyncio.sleep", AsyncMock()),
        pytest.raises(httpx.TimeoutException),
    ):
        await run_mint_operation(always_timeout, mint_url=mint_url)

    assert MintRateGuard.get(mint_url).cooldown_remaining() > 29
    MintRateGuard._guards.pop(mint_url, None)


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


def _rate_limited_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://mint:3338/v1/checkstate")
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


@pytest.mark.asyncio
async def test_rate_limit_outside_passive_scope_opens_shared_cooldown() -> None:
    mint = "http://passive-control:3338"
    MintRateGuard._guards.pop(mint, None)
    operation = AsyncMock(side_effect=_rate_limited_error())

    with pytest.raises(httpx.HTTPStatusError):
        await run_mint_operation(operation, mint_url=mint, retry_on_rate_limit=False)

    assert MintRateGuard.get(mint).cooldown_remaining() > 0
    MintRateGuard._guards.pop(mint, None)


@pytest.mark.asyncio
async def test_passive_read_rate_limit_leaves_shared_cooldown_untouched() -> None:
    mint = "http://passive:3338"
    MintRateGuard._guards.pop(mint, None)
    operation = AsyncMock(side_effect=_rate_limited_error())

    with pytest.raises(httpx.HTTPStatusError):
        async with passive_mint_reads():
            await run_mint_operation(operation, mint_url=mint)

    guard = MintRateGuard.get(mint)
    operation.assert_awaited_once()
    assert guard.cooldown_remaining() == 0
    assert guard._consecutive_rate_limits == 0
    MintRateGuard._guards.pop(mint, None)


@pytest.mark.asyncio
async def test_passive_probe_rate_limit_does_not_escalate_backoff() -> None:
    mint = "http://passive-probe:3338"
    MintRateGuard._guards.pop(mint, None)
    guard = MintRateGuard.get(mint)
    guard.apply_rate_limit_cooldown(0)
    guard._cooldown_until = 0.0
    operation = AsyncMock(side_effect=_rate_limited_error())

    with pytest.raises(httpx.HTTPStatusError):
        async with passive_mint_reads():
            await run_mint_operation(operation, mint_url=mint)

    operation.assert_awaited_once()
    assert guard.cooldown_remaining() == 0
    assert guard._consecutive_rate_limits == 1
    assert guard._needs_probe is True
    MintRateGuard._guards.pop(mint, None)


@pytest.mark.asyncio
async def test_passive_reads_fail_fast_during_shared_cooldown() -> None:
    mint = "http://passive-cooldown:3338"
    MintRateGuard._guards.pop(mint, None)
    MintRateGuard.get(mint).apply_cooldown(3600, reason="rate_limited")
    operation = AsyncMock()

    with pytest.raises(MintCooldownError):
        async with passive_mint_reads():
            await run_mint_operation(operation, mint_url=mint)

    operation.assert_not_awaited()
    MintRateGuard._guards.pop(mint, None)


@pytest.mark.asyncio
async def test_passive_reads_are_spaced_per_mint() -> None:
    mint = "http://passive-spacing:3338"
    MintRateGuard._guards.pop(mint, None)
    operation = AsyncMock(return_value="ok")

    with (
        patch("routstr.mint._PASSIVE_REQUEST_SPACING_SECONDS", 0.05),
        patch("routstr.mint.asyncio.sleep", AsyncMock()) as sleep,
    ):
        async with passive_mint_reads():
            await run_mint_operation(operation, mint_url=mint)
            await run_mint_operation(operation, mint_url=mint)

    assert operation.await_count == 2
    sleep.assert_awaited_once()
    assert sleep.await_args is not None
    assert 0 < sleep.await_args.args[0] <= 0.05
    MintRateGuard._guards.pop(mint, None)
