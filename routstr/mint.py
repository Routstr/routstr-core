"""Shared policy for bounded, rate-aware Cashu mint API operations."""

from __future__ import annotations

import asyncio
import socket
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncGenerator, Awaitable, Callable

import httpx

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

_fail_fast_depth: ContextVar[int] = ContextVar("mint_fail_fast_depth", default=0)


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
    """Limit concurrency and remember per-mint cooldown/probe state."""

    _guards: dict[str, "MintRateGuard"] = {}

    @classmethod
    def get(cls, mint_url: str) -> "MintRateGuard":
        concurrency = settings.mint_max_concurrency
        guard = cls._guards.get(mint_url)
        if guard is None or guard._max_concurrency != concurrency:
            guard = cls(mint_url, concurrency)
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

    def apply_cooldown(self, delay: float, *, reason: str | None = None) -> None:
        deadline = time.monotonic() + max(0.0, delay)
        if deadline >= self._cooldown_until:
            self._cooldown_until = deadline
            if reason is not None:
                self._cooldown_reason = reason
        elif self._cooldown_reason is None and reason is not None:
            self._cooldown_reason = reason
        self._needs_probe = True

    def apply_rate_limit_cooldown(self, retry_after: float | None = None) -> float:
        remaining = self.cooldown_remaining()
        if remaining > 0 and self._cooldown_reason == "rate_limited":
            minimum = min(
                _MINT_RATE_LIMIT_MAX_COOLDOWN_SECONDS,
                max(_MINT_RATE_LIMIT_BASE_COOLDOWN_SECONDS, retry_after or 0.0),
            )
            if minimum > remaining:
                self.apply_cooldown(minimum, reason="rate_limited")
                return minimum
            return remaining

        self._consecutive_rate_limits += 1
        base = max(_MINT_RATE_LIMIT_BASE_COOLDOWN_SECONDS, retry_after or 0.0)
        multiplier = 2 ** min(self._consecutive_rate_limits - 1, 10)
        delay = min(_MINT_RATE_LIMIT_MAX_COOLDOWN_SECONDS, base * multiplier)
        self.apply_cooldown(delay, reason="rate_limited")
        return delay

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    def cooldown_reason(self) -> str | None:
        return self._cooldown_reason if self.cooldown_remaining() > 0 else None

    def _raise_if_wait_forbidden(self) -> None:
        if _fail_fast_depth.get() and (
            self._needs_probe or self.cooldown_remaining() > 0
        ):
            raise MintCooldownError(self._mint_url, self.cooldown_remaining())

    async def _wait_for_cooldown(self) -> None:
        while True:
            self._raise_if_wait_forbidden()
            deadline = self._cooldown_until
            wait = max(0.0, deadline - time.monotonic())
            if wait <= 0:
                return
            logger.debug(
                "Mint rate guard: cooling down",
                extra={"mint_url": self._mint_url, "wait_seconds": round(wait, 2)},
            )
            await asyncio.sleep(wait)
            if self._cooldown_until <= deadline:
                return

    async def _run_probe(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        await self._wait_for_cooldown()
        logger.info(
            "Mint cooldown ended; sending one probe request",
            extra={"event": "mint_cooldown_probe_started", "mint_url": self._mint_url},
        )
        try:
            result = await factory()
        except Exception as error:
            if is_mint_rate_limited(error):
                retry_after = None
                if isinstance(error, httpx.HTTPStatusError):
                    retry_after = parse_retry_after(error.response.headers)
                delay = max(
                    _MINT_RATE_LIMIT_BASE_COOLDOWN_SECONDS,
                    retry_after or 0.0,
                )
                self.apply_cooldown(delay, reason="rate_limited")
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

        self._needs_probe = False
        self._cooldown_until = 0.0
        self._cooldown_reason = None
        self._consecutive_rate_limits = 0
        logger.info(
            "Mint cooldown probe succeeded; restoring normal concurrency",
            extra={
                "event": "mint_cooldown_probe_succeeded",
                "mint_url": self._mint_url,
            },
        )
        return result

    async def run(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        while True:
            self._raise_if_wait_forbidden()
            if self._needs_probe or self.cooldown_remaining() > 0:
                async with self._probe_lock:
                    self._raise_if_wait_forbidden()
                    if self.cooldown_remaining() > 0:
                        self._needs_probe = True
                    if self._needs_probe:
                        return await self._run_probe(factory)
                continue

            if self._semaphore is None:
                return await factory()
            async with self._semaphore:
                self._raise_if_wait_forbidden()
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
