"""Shared policy for bounded, rate-aware Cashu mint API operations."""

from __future__ import annotations

import asyncio
import math
import socket
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable

import httpx

from . import node_coordination
from .core.logging import get_logger
from .core.settings import settings

logger = get_logger(__name__)

MINT_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.NetworkError,
    httpx.TimeoutException,
    ConnectionError,
    socket.gaierror,
    asyncio.TimeoutError,
)

MINT_TRANSPORT_COOLDOWN_SECONDS = 30.0
_MINT_RATE_LIMIT_BASE_COOLDOWN_SECONDS = 60.0
_MINT_RATE_LIMIT_MAX_COOLDOWN_SECONDS = 7 * 60 * 60


def _read_boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None
    return value or None


# Monotonic clocks are shared by processes on one boot but reset on reboot.
_NODE_BOOT_ID = _read_boot_id()
_fail_fast_depth: ContextVar[int] = ContextVar("mint_fail_fast_depth", default=0)


class MintError(Exception):
    """Structured error response returned by a Cashu mint."""

    def __init__(self, detail: Any, code: Any | None = None):
        self.detail = detail
        self.code = code
        message = f"Mint Error: {detail}"
        if code is not None:
            message += f" (Code: {code})"
        super().__init__(message)


class MintRateLimitedError(httpx.HTTPStatusError):
    """Typed boundary error preserving a Cashu mint's HTTP 429 response."""


class MintCooldownError(Exception):
    """A mint is cooling down and this operation must not wait."""

    def __init__(self, mint_url: str, retry_after_seconds: float):
        self.mint_url = mint_url
        self.retry_after_seconds = max(0.0, retry_after_seconds)
        super().__init__(
            f"Mint {mint_url} is cooling down; retry after "
            f"{self.retry_after_seconds:.2f}s"
        )


@asynccontextmanager
async def fail_fast_mint_operations() -> AsyncGenerator[None, None]:
    """Make mint cooldown/probe waits fail fast in the current task.

    Wallet mutation code holds a process-wide file lock. It enters this scope so
    an existing mint cooldown can never turn that lock into a multi-hour wait.
    """

    token = _fail_fast_depth.set(_fail_fast_depth.get() + 1)
    try:
        yield
    finally:
        _fail_fast_depth.reset(token)


class MintRateGuard:
    """Apply one node-wide concurrency and cooldown policy per mint."""

    _guards: dict[str, "MintRateGuard"] = {}

    @classmethod
    def get(cls, mint_url: str) -> "MintRateGuard":
        concurrency = settings.mint_max_concurrency
        guard = cls._guards.get(mint_url)
        if guard is None or guard._max_concurrency != concurrency:
            guard = cls(mint_url, concurrency)
            with node_coordination.blocking_lock(guard._state_lock_path):
                state = guard._read_shared_state()
                if state is not None:
                    guard._write_local_state(*state[:4])
            cls._guards[mint_url] = guard
        return guard

    def __init__(self, mint_url: str, max_concurrency: int):
        self._mint_url = mint_url
        self._max_concurrency = max_concurrency
        self._semaphore = (
            asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None
        )
        self._cooldown_until = 0.0
        self._cooldown_reason: str | None = None
        self._consecutive_rate_limits = 0
        self._needs_probe = False
        self._probe_lock = asyncio.Lock()
        self._key = node_coordination.state_key(mint_url)

    @property
    def _directory(self) -> Path:
        return node_coordination.NODE_STATE_DIR / "mints" / self._key

    @property
    def _state_path(self) -> Path:
        return self._directory / "state.json"

    @property
    def _state_lock_path(self) -> Path:
        return self._directory / "state.lock"

    @property
    def _probe_lock_path(self) -> Path:
        return self._directory / "probe.lock"

    def _read_shared_state(
        self,
    ) -> tuple[float, str | None, int, bool, int] | None:
        value = node_coordination.read_json(self._state_path)
        version = value.get("version") if value is not None else None
        if value is None or version not in (1, 2, 3):
            return None
        try:
            consecutive = int(value["consecutive_rate_limits"])
            needs_probe = value["needs_probe"]
            generation = int(value.get("generation", 0))
            reason = value.get("reason")
            same_boot = (
                version == 3
                and _NODE_BOOT_ID is not None
                and value.get("boot_id") == _NODE_BOOT_ID
            )
            if same_boot:
                cooldown_until = float(value["monotonic_until"])
                now = time.monotonic()
            else:
                wall_until = float(value["cooldown_until"])
                wall_now = time.time()
                if (
                    not math.isfinite(wall_until)
                    or wall_until < 0
                    or wall_until
                    > wall_now + _MINT_RATE_LIMIT_MAX_COOLDOWN_SECONDS + 60
                ):
                    return None
                cooldown_until = time.monotonic() + max(0.0, wall_until - wall_now)
                now = time.monotonic()
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if (
            not math.isfinite(cooldown_until)
            or cooldown_until < 0
            or cooldown_until > now + _MINT_RATE_LIMIT_MAX_COOLDOWN_SECONDS + 60
            or not 0 <= consecutive <= 1024
            or not 0 <= generation <= 2**63 - 1
            or not isinstance(needs_probe, bool)
            or (reason is not None and not isinstance(reason, str))
        ):
            return None
        return cooldown_until, reason, consecutive, needs_probe, generation

    def _write_shared_state(
        self,
        cooldown_until: float,
        reason: str | None,
        consecutive: int,
        needs_probe: bool,
        generation: int,
    ) -> None:
        remaining = max(0.0, cooldown_until - time.monotonic())
        node_coordination.write_json(
            self._state_path,
            {
                "version": 3,
                "boot_id": _NODE_BOOT_ID,
                "monotonic_until": cooldown_until,
                "cooldown_until": time.time() + remaining,
                "reason": reason,
                "consecutive_rate_limits": consecutive,
                "needs_probe": needs_probe,
                "generation": generation,
            },
        )
        self._cooldown_until = cooldown_until
        self._cooldown_reason = reason
        self._consecutive_rate_limits = consecutive
        self._needs_probe = needs_probe

    def _sync_shared_state(self) -> None:
        state = self._read_shared_state()
        if state is None:
            return
        self._write_local_state(*state[:4])

    def _write_local_state(
        self,
        cooldown_until: float,
        reason: str | None,
        consecutive: int,
        needs_probe: bool,
    ) -> None:
        self._cooldown_until = cooldown_until
        self._cooldown_reason = reason
        self._consecutive_rate_limits = consecutive
        self._needs_probe = needs_probe

    def apply_cooldown(self, delay: float, *, reason: str | None = None) -> None:
        delay = max(0.0, delay)
        with node_coordination.blocking_lock(self._state_lock_path):
            state = self._read_shared_state()
            current_until, current_reason, consecutive, _, generation = state or (
                0.0,
                None,
                self._consecutive_rate_limits,
                False,
                0,
            )
            deadline = time.monotonic() + delay
            if deadline >= current_until:
                current_until = deadline
                if reason is not None:
                    current_reason = reason
            elif current_reason is None and reason is not None:
                current_reason = reason
            self._write_shared_state(
                current_until, current_reason, consecutive, True, generation + 1
            )

    def apply_rate_limit_cooldown(self, retry_after: float | None = None) -> float:
        with node_coordination.blocking_lock(self._state_lock_path):
            state = self._read_shared_state()
            current_until, reason, consecutive, _, generation = state or (
                0.0,
                None,
                0,
                False,
                0,
            )
            remaining = max(0.0, current_until - time.monotonic())
            minimum = min(
                _MINT_RATE_LIMIT_MAX_COOLDOWN_SECONDS,
                max(_MINT_RATE_LIMIT_BASE_COOLDOWN_SECONDS, retry_after or 0.0),
            )
            if remaining > 0 and reason == "rate_limited":
                delay = max(remaining, minimum)
            else:
                consecutive += 1
                multiplier = 2 ** min(consecutive - 1, 10)
                delay = min(_MINT_RATE_LIMIT_MAX_COOLDOWN_SECONDS, minimum * multiplier)
            self._write_shared_state(
                time.monotonic() + delay,
                "rate_limited",
                consecutive,
                True,
                generation + 1,
            )
            return delay

    def cooldown_remaining(self) -> float:
        self._sync_shared_state()
        return max(0.0, self._cooldown_until - time.monotonic())

    def cooldown_reason(self) -> str | None:
        return self._cooldown_reason if self.cooldown_remaining() > 0 else None

    def _raise_if_wait_forbidden(self) -> None:
        remaining = self.cooldown_remaining()
        if _fail_fast_depth.get() and remaining > 0:
            raise MintCooldownError(self._mint_url, remaining)

    async def _wait_for_cooldown(self) -> None:
        while True:
            self._raise_if_wait_forbidden()
            shared_state = self._read_shared_state()
            if shared_state is not None:
                self._write_local_state(*shared_state[:4])
            wait = max(0.0, self._cooldown_until - time.monotonic())
            deadline = self._cooldown_until
            shared_deadline = shared_state[0] if shared_state is not None else None
            generation = shared_state[4] if shared_state is not None else None
            if wait <= 0:
                return
            logger.debug(
                "Mint rate guard: cooling down",
                extra={"mint_url": self._mint_url, "wait_seconds": round(wait, 2)},
            )
            await asyncio.sleep(wait)
            if shared_deadline is not None and generation is not None:
                with node_coordination.blocking_lock(self._state_lock_path):
                    current = self._read_shared_state()
                    if (
                        current is not None
                        and current[4] == generation
                        and current[0] <= time.monotonic()
                    ):
                        self._write_shared_state(
                            0.0, current[1], current[2], True, generation + 1
                        )
            self._sync_shared_state()
            if self._cooldown_until <= deadline:
                return

    @asynccontextmanager
    async def _node_slot(self) -> AsyncGenerator[None, None]:
        if self._max_concurrency <= 0:
            yield
            return
        fd: int | None = None
        try:
            while fd is None:
                for slot in range(self._max_concurrency):
                    fd = node_coordination.try_lock(
                        self._directory / f"slot-{slot}.lock"
                    )
                    if fd is not None:
                        break
                if fd is None:
                    await asyncio.sleep(0.05)
            yield
        finally:
            if fd is not None:
                node_coordination.unlock(fd)

    async def _acquire_probe_lock(self) -> int:
        while True:
            fd = node_coordination.try_lock(self._probe_lock_path)
            if fd is not None:
                return fd
            if _fail_fast_depth.get():
                raise MintCooldownError(self._mint_url, self.cooldown_remaining())
            await asyncio.sleep(0.05)

    def _clear_shared_state(self, expected_generation: int) -> bool:
        with node_coordination.blocking_lock(self._state_lock_path):
            state = self._read_shared_state()
            if state is None:
                if expected_generation != 0:
                    return False
                self._write_shared_state(0.0, None, 0, False, 1)
                return True
            if state[4] != expected_generation:
                self._write_local_state(*state[:4])
                return False
            self._write_shared_state(0.0, None, 0, False, expected_generation + 1)
            return True

    async def _run_probe(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        await self._wait_for_cooldown()
        if self._read_shared_state() is None:
            self._cooldown_until = 0.0
            self._needs_probe = True
        fd: int | None = await self._acquire_probe_lock()
        try:
            self._sync_shared_state()
            if self.cooldown_remaining() > 0:
                assert fd is not None
                node_coordination.unlock(fd)
                fd = None
                await self._wait_for_cooldown()
                return await self._run_probe(factory)
            if not self._needs_probe:
                assert fd is not None
                node_coordination.unlock(fd)
                fd = None
                return await self.run(factory)
            state = self._read_shared_state()
            probe_generation = state[4] if state is not None else 0
            logger.info(
                "Mint cooldown ended; sending one probe request",
                extra={
                    "event": "mint_cooldown_probe_started",
                    "mint_url": self._mint_url,
                },
            )
            try:
                async with self._node_slot():
                    result = await factory()
            except Exception as error:
                if is_mint_rate_limited(error):
                    retry_after = None
                    if isinstance(error, httpx.HTTPStatusError):
                        retry_after = parse_retry_after(error.response.headers)
                    self.apply_rate_limit_cooldown(retry_after)
                else:
                    self.apply_cooldown(1.0)
                logger.warning(
                    "Mint cooldown probe failed",
                    extra={
                        "event": "mint_cooldown_probe_failed",
                        "mint_url": self._mint_url,
                        "error": str(error),
                        "error_type": type(error).__name__,
                        "cooldown_seconds": round(self.cooldown_remaining(), 2),
                        "consecutive_rate_limits": self._consecutive_rate_limits,
                    },
                )
                raise
            state_cleared = self._clear_shared_state(probe_generation)
            logger.info(
                "Mint cooldown probe succeeded",
                extra={
                    "event": "mint_cooldown_probe_succeeded",
                    "mint_url": self._mint_url,
                    "state_cleared": state_cleared,
                },
            )
            return result
        finally:
            if fd is not None:
                node_coordination.unlock(fd)

    async def run(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        while True:
            self._raise_if_wait_forbidden()
            self._sync_shared_state()
            if self._needs_probe or self.cooldown_remaining() > 0:
                if _fail_fast_depth.get() and self._probe_lock.locked():
                    raise MintCooldownError(self._mint_url, self.cooldown_remaining())
                async with self._probe_lock:
                    self._raise_if_wait_forbidden()
                    self._sync_shared_state()
                    if self._needs_probe or self.cooldown_remaining() > 0:
                        return await self._run_probe(factory)
                continue

            if self._semaphore is None:
                async with self._node_slot():
                    self._sync_shared_state()
                    if self._needs_probe:
                        continue
                    return await factory()
            async with self._semaphore:
                async with self._node_slot():
                    self._raise_if_wait_forbidden()
                    self._sync_shared_state()
                    if self._needs_probe:
                        continue
                    return await factory()


def mint_cooldown_remaining(mint_url: str) -> float:
    return MintRateGuard.get(mint_url).cooldown_remaining()


def mint_cooldown_reason(mint_url: str) -> str | None:
    return MintRateGuard.get(mint_url).cooldown_reason()


def is_mint_rate_limited(error: BaseException) -> bool:
    """Return whether an exception chain represents HTTP 429/cooldown."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, MintCooldownError):
            return True
        if isinstance(current, httpx.HTTPStatusError):
            if current.response.status_code == 429:
                return True
        current = current.__cause__ or current.__context__
    return False


def parse_retry_after(headers: Any) -> float | None:
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def run_mint_operation(
    factory: Callable[[], Awaitable[Any]],
    *,
    op_name: str = "mint_operation",
    mint_url: str = "",
    retry_timeouts: bool = True,
    retry_on_rate_limit: bool = True,
) -> Any:
    """Run one mint operation with bounded concurrency and adaptive cooldown."""

    guard = MintRateGuard.get(mint_url) if mint_url else None
    timeout = settings.mint_operation_timeout_seconds
    max_attempts = settings.mint_retry_max_attempts + 1

    async def timed_factory() -> Any:
        if timeout > 0:
            return await asyncio.wait_for(factory(), timeout=timeout)
        return await factory()

    async def invoke() -> Any:
        if guard is not None:
            return await guard.run(timed_factory)
        return await timed_factory()

    for attempt in range(max_attempts):
        try:
            return await invoke()
        except MintCooldownError:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            if retry_timeouts and attempt < max_attempts - 1:
                backoff = (2**attempt) + (time.monotonic() % 1.0)
                logger.warning(
                    "Mint operation timed out, retrying",
                    extra={
                        "op_name": op_name,
                        "mint_url": mint_url,
                        "attempt": attempt + 1,
                        "backoff_seconds": round(backoff, 2),
                    },
                )
                await asyncio.sleep(backoff)
                continue
            raise httpx.TimeoutException(
                f"{op_name} timed out (attempts: {attempt + 1})"
            ) from exc
        except Exception as exc:
            if not is_mint_rate_limited(exc):
                raise

            backoff = (2**attempt) + (time.monotonic() % 1.0)
            if isinstance(exc, httpx.HTTPStatusError):
                retry_after = parse_retry_after(exc.response.headers)
                if retry_after is not None:
                    backoff = max(retry_after, backoff)
            cooldown = backoff
            if guard is not None:
                cooldown = guard.apply_rate_limit_cooldown(backoff)

            if not retry_on_rate_limit:
                logger.warning(
                    "Mint rate-limited, skipping retries for fallback",
                    extra={
                        "op_name": op_name,
                        "mint_url": mint_url,
                        "cooldown_seconds": round(cooldown, 2),
                        "consecutive_rate_limits": guard._consecutive_rate_limits
                        if guard is not None
                        else attempt + 1,
                    },
                )
                raise

            if attempt >= max_attempts - 1:
                raise
            logger.warning(
                "Mint rate-limited, applying cooldown",
                extra={
                    "op_name": op_name,
                    "mint_url": mint_url,
                    "attempt": attempt + 1,
                    "cooldown_seconds": round(cooldown, 2),
                    "consecutive_rate_limits": guard._consecutive_rate_limits
                    if guard is not None
                    else attempt + 1,
                },
            )
            if guard is None:
                await asyncio.sleep(cooldown)

    raise RuntimeError(f"{op_name}: exhausted retries unexpectedly")
