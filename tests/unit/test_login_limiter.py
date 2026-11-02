"""Tests for the admin login limiter (per-address lockout, global check cap)."""

from routstr.core.login_limiter import LoginLimiter, client_key


def _limiter(now: list[float], **kwargs: int | float) -> LoginLimiter:
    return LoginLimiter(clock=lambda: now[0], **kwargs)  # type: ignore[arg-type]


def test_failures_below_the_allowance_do_not_lock() -> None:
    now = [0.0]
    limiter = _limiter(now, free_attempts=3)
    limiter.record_failure("a")
    limiter.record_failure("a")
    assert limiter.retry_after("a") is None


def test_lockout_doubles_per_failure_up_to_the_cap() -> None:
    now = [0.0]
    limiter = _limiter(
        now, free_attempts=2, base_lockout_seconds=2, max_lockout_seconds=10
    )
    limiter.record_failure("a")
    limiter.record_failure("a")
    assert limiter.retry_after("a") == 2
    limiter.record_failure("a")
    assert limiter.retry_after("a") == 4
    limiter.record_failure("a")
    assert limiter.retry_after("a") == 8
    limiter.record_failure("a")
    assert limiter.retry_after("a") == 10

    now[0] += 10
    assert limiter.retry_after("a") is None


def test_lockout_is_per_address_and_cleared_by_success() -> None:
    now = [0.0]
    limiter = _limiter(now, free_attempts=1)
    limiter.record_failure("a")
    assert limiter.retry_after("a") is not None
    assert limiter.retry_after("b") is None

    limiter.record_success("a")
    assert limiter.retry_after("a") is None


def test_old_failures_are_forgotten() -> None:
    now = [0.0]
    limiter = _limiter(now, free_attempts=2)
    limiter.record_failure("a")
    now[0] += 7200
    limiter.record_failure("a")
    assert limiter.retry_after("a") is None


def test_tracked_addresses_are_bounded() -> None:
    now = [0.0]
    limiter = _limiter(now, free_attempts=1, max_tracked=2)
    for address in ("a", "b", "c"):
        limiter.record_failure(address)
    assert limiter.retry_after("a") is None
    assert limiter.retry_after("b") is not None
    assert limiter.retry_after("c") is not None


def test_concurrent_checks_are_capped() -> None:
    limiter = LoginLimiter(max_concurrent_checks=2)
    assert limiter.try_start_check()
    assert limiter.try_start_check()
    assert not limiter.try_start_check()
    limiter.finish_check()
    assert limiter.try_start_check()


def test_client_key_groups_ipv6_by_64() -> None:
    assert client_key("2001:db8:1:2::1") == client_key("2001:db8:1:2:ffff::9")
    assert client_key("2001:db8:1:2::1") != client_key("2001:db8:1:3::1")
    assert client_key("192.0.2.7") == "192.0.2.7"
    assert client_key(None) == "unknown"
