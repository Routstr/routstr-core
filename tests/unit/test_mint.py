import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from cashu.core.base import Unit

from routstr import node_coordination
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


@pytest.mark.asyncio
async def test_cancelled_probe_releases_node_lock() -> None:
    mint_url = "https://mint.test-cancelled-probe"
    guard = MintRateGuard(mint_url, max_concurrency=1)
    guard.apply_cooldown(0, reason="rate_limited")
    started = asyncio.Event()

    async def blocked() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(guard.run(blocked))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    operation = AsyncMock(return_value="recovered")
    assert await MintRateGuard(mint_url, 1).run(operation) == "recovered"
    operation.assert_awaited_once()


def test_corrupt_shared_state_fails_open() -> None:
    guard = MintRateGuard("https://mint.test-corrupt-state", max_concurrency=1)
    guard._state_path.parent.mkdir(parents=True, exist_ok=True)
    guard._state_path.write_text("not-json")

    assert guard.cooldown_remaining() == 0


@pytest.mark.parametrize("wall_step", [3600.0, -3600.0])
def test_shared_cooldown_ignores_wall_clock_steps(wall_step: float) -> None:
    clock = {"wall": 10_000.0, "monotonic": 100.0}
    mint_url = f"https://mint.test-wall-step-{wall_step}"

    with (
        patch("routstr.mint._NODE_BOOT_ID", "boot-a"),
        patch("routstr.mint.time.time", side_effect=lambda: clock["wall"]),
        patch(
            "routstr.mint.time.monotonic",
            side_effect=lambda: clock["monotonic"],
        ),
    ):
        MintRateGuard(mint_url, 1).apply_cooldown(60, reason="rate_limited")
        clock["wall"] += 10 + wall_step
        clock["monotonic"] += 10

        fresh = MintRateGuard(mint_url, 1)
        assert fresh.cooldown_remaining() == pytest.approx(50)
        assert fresh.cooldown_reason() == "rate_limited"


def test_backward_wall_step_does_not_reject_maximum_cooldown() -> None:
    clock = {"wall": 10_000.0, "monotonic": 100.0}
    mint_url = "https://mint.test-wall-validation"
    maximum = 7 * 60 * 60

    with (
        patch("routstr.mint._NODE_BOOT_ID", "boot-a"),
        patch("routstr.mint.time.time", side_effect=lambda: clock["wall"]),
        patch(
            "routstr.mint.time.monotonic",
            side_effect=lambda: clock["monotonic"],
        ),
    ):
        MintRateGuard(mint_url, 1).apply_cooldown(maximum, reason="rate_limited")
        clock["wall"] -= 120
        clock["monotonic"] += 1

        fresh = MintRateGuard(mint_url, 1)
        assert fresh.cooldown_remaining() == pytest.approx(maximum - 1)


def test_shared_cooldown_falls_back_to_wall_clock_after_reboot() -> None:
    clock = {"wall": 10_000.0, "monotonic": 100.0}
    mint_url = "https://mint.test-reboot-fallback"

    with (
        patch("routstr.mint._NODE_BOOT_ID", "boot-a"),
        patch("routstr.mint.time.time", side_effect=lambda: clock["wall"]),
        patch(
            "routstr.mint.time.monotonic",
            side_effect=lambda: clock["monotonic"],
        ),
    ):
        MintRateGuard(mint_url, 1).apply_cooldown(60, reason="transport")

    clock = {"wall": 10_010.0, "monotonic": 5.0}
    with (
        patch("routstr.mint._NODE_BOOT_ID", "boot-b"),
        patch("routstr.mint.time.time", side_effect=lambda: clock["wall"]),
        patch(
            "routstr.mint.time.monotonic",
            side_effect=lambda: clock["monotonic"],
        ),
    ):
        fresh = MintRateGuard(mint_url, 1)
        assert fresh.cooldown_remaining() == pytest.approx(50)
        assert fresh.cooldown_reason() == "transport"


def test_legacy_shared_cooldown_uses_bounded_wall_clock_fallback() -> None:
    guard = MintRateGuard("https://mint.test-legacy-state", 1)
    node_coordination.write_json(
        guard._state_path,
        {
            "version": 2,
            "cooldown_until": time.time() + 30,
            "reason": "transport",
            "consecutive_rate_limits": 0,
            "needs_probe": True,
            "generation": 4,
        },
    )

    assert guard.cooldown_remaining() == pytest.approx(30, abs=1)
    assert guard._needs_probe is True


async def test_guard_concurrency_change_uses_current_shared_state() -> None:
    from routstr.core.settings import settings

    mint_url = "https://mint.test-concurrency-carryover"
    with patch.object(settings, "mint_max_concurrency", 2):
        stale = MintRateGuard.get(mint_url)
        stale.apply_rate_limit_cooldown()
        stale._consecutive_rate_limits = 7

    current = MintRateGuard(mint_url, 2)
    state = current._read_shared_state()
    assert state is not None
    assert current._clear_shared_state(state[4])

    with patch.object(settings, "mint_max_concurrency", 5):
        rebuilt = MintRateGuard.get(mint_url)

    assert rebuilt is not stale
    assert rebuilt.cooldown_remaining() == 0
    assert rebuilt._needs_probe is False
    assert rebuilt._consecutive_rate_limits == 0


async def test_delayed_waiter_does_not_restore_cleared_probe_state() -> None:
    mint_url = "https://mint.test-delayed-waiter"
    guard = MintRateGuard(mint_url, 1)
    guard.apply_cooldown(60, reason="rate_limited")

    async def clear_while_waiting(_: float) -> None:
        other = MintRateGuard(mint_url, 1)
        state = other._read_shared_state()
        assert state is not None
        assert other._clear_shared_state(state[4])

    with patch("routstr.mint.asyncio.sleep", clear_while_waiting):
        await guard._wait_for_cooldown()

    state = guard._read_shared_state()
    assert state is not None
    assert state[3] is False


async def test_waiter_does_not_restore_state_cleared_between_reads() -> None:
    mint_url = "https://mint.test-waiter-cleared-between-reads"
    guard = MintRateGuard(mint_url, 1)
    guard.apply_cooldown(60, reason="rate_limited")
    read_shared_state = guard._read_shared_state
    read_calls = 0
    state_cleared = False

    def read_and_clear_after_second_snapshot():
        nonlocal read_calls, state_cleared
        read_calls += 1
        state = read_shared_state()
        if read_calls == 2:
            other = MintRateGuard(mint_url, 1)
            current = other._read_shared_state()
            assert current is not None
            assert other._clear_shared_state(current[4])
            state_cleared = True
        return state

    with (
        patch.object(guard, "_read_shared_state", read_and_clear_after_second_snapshot),
        patch("routstr.mint.asyncio.sleep", new=AsyncMock()),
    ):
        await guard._wait_for_cooldown()

    state = read_shared_state()
    assert state_cleared
    assert state is not None
    assert state[3] is False


async def test_waiter_does_not_clear_extended_cooldown() -> None:
    mint_url = "https://mint.test-waiter-extension"
    guard = MintRateGuard(mint_url, 1)
    guard.apply_cooldown(10, reason="rate_limited")
    sleep_calls = 0

    async def extend_then_stop(_: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            MintRateGuard(mint_url, 1).apply_cooldown(60, reason="transport")
        else:
            raise asyncio.CancelledError

    with (
        patch("routstr.mint.asyncio.sleep", side_effect=extend_then_stop),
        pytest.raises(asyncio.CancelledError),
    ):
        await guard._wait_for_cooldown()

    state = guard._read_shared_state()
    assert state is not None
    assert state[0] > time.monotonic()
    assert state[1] == "transport"
    assert state[3] is True


async def test_successful_probe_keeps_newer_cooldown() -> None:
    mint_url = "https://mint.test-probe-generation"
    guard = MintRateGuard(mint_url, 1)
    guard.apply_cooldown(0, reason="rate_limited")

    async def probe() -> str:
        MintRateGuard(mint_url, 1).apply_cooldown(60, reason="transport")
        return "ok"

    assert await guard.run(probe) == "ok"
    state = guard._read_shared_state()
    assert state is not None
    assert state[0] > 0
    assert state[1] == "transport"
    assert state[3] is True
