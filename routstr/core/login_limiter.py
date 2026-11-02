"""Throttle the unauthenticated admin login.

Each password check is a scrypt derivation (tens of milliseconds of CPU). The
login route is the only admin route that needs no token, so without a limit
anyone can keep the node's CPU busy with wrong passwords, and guess passwords
as fast as the CPU allows. Two limits apply, both in process memory:

- per client address, consecutive failures past a small allowance lock the
  address out for a window that doubles with every further failure;
- across all clients, only a few checks run at once. A caller that finds them
  all busy gets 429 instead of queueing more work.
"""

import ipaddress
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

FREE_ATTEMPTS = 5
BASE_LOCKOUT_SECONDS = 2.0
MAX_LOCKOUT_SECONDS = 300.0
# An address's failures are forgotten this long after its last failure.
FAILURE_MEMORY_SECONDS = 3600.0
MAX_CONCURRENT_CHECKS = 2
MAX_TRACKED_ADDRESSES = 10_000


@dataclass
class _Failures:
    count: int = 0
    last_failure: float = 0.0
    locked_until: float = 0.0


def client_key(host: str | None) -> str:
    """Group an address with the others its owner controls.

    An IPv6 client usually holds a whole /64, so it counts as one address.
    """
    if not host:
        return "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class LoginLimiter:
    def __init__(
        self,
        *,
        free_attempts: int = FREE_ATTEMPTS,
        base_lockout_seconds: float = BASE_LOCKOUT_SECONDS,
        max_lockout_seconds: float = MAX_LOCKOUT_SECONDS,
        max_concurrent_checks: int = MAX_CONCURRENT_CHECKS,
        max_tracked: int = MAX_TRACKED_ADDRESSES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.free_attempts = free_attempts
        self.base_lockout_seconds = base_lockout_seconds
        self.max_lockout_seconds = max_lockout_seconds
        self.max_concurrent_checks = max_concurrent_checks
        self.max_tracked = max_tracked
        self._clock = clock
        # Insertion-ordered by last failure, so the oldest entry is first.
        self._failures: dict[str, _Failures] = {}
        self._checks_running = 0

    def retry_after(self, key: str) -> int | None:
        """Seconds until ``key`` may try again, or None if it may try now."""
        entry = self._failures.get(key)
        if entry is None:
            return None
        remaining = entry.locked_until - self._clock()
        if remaining <= 0:
            return None
        return max(1, math.ceil(remaining))

    def record_failure(self, key: str) -> None:
        now = self._clock()
        entry = self._failures.pop(key, None)
        if entry is None or now - entry.last_failure > FAILURE_MEMORY_SECONDS:
            entry = _Failures()
        entry.count += 1
        entry.last_failure = now
        if entry.count >= self.free_attempts:
            exponent = min(entry.count - self.free_attempts, 30)
            lockout = min(
                self.base_lockout_seconds * 2**exponent, self.max_lockout_seconds
            )
            entry.locked_until = now + lockout
        self._failures[key] = entry
        while len(self._failures) > self.max_tracked:
            del self._failures[next(iter(self._failures))]

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)

    def try_start_check(self) -> bool:
        """Claim a password-check slot; False when all are busy."""
        if self._checks_running >= self.max_concurrent_checks:
            return False
        self._checks_running += 1
        return True

    def finish_check(self) -> None:
        self._checks_running = max(0, self._checks_running - 1)

    def reset(self) -> None:
        self._failures.clear()
        self._checks_running = 0


login_limiter = LoginLimiter()
