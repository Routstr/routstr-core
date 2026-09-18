"""In-memory negative cache for terminally failed Cashu token redemptions.

A dead token (already spent, malformed, zero value) presented as a bearer key
triggers a full redemption attempt against the issuing mint on *every* request,
because no ``api_keys`` row survives the failed attempt. Polling clients that
never back off turn one dead token into thousands of pointless mint calls per
day. This cache remembers terminal redemption failures by token hash so
repeated presentations are rejected locally with the same error the mint
attempt would have produced.

Only failures whose classification code is in :data:`TERMINAL_REDEMPTION_CODES`
are cached — transient failures (mint unreachable, rate-limited, cooldown) must
never be cached, or a brief mint outage would poison valid tokens.

The cache is deliberately in-memory (bounded LRU with TTL) rather than a
database row: persisting a row per failed token would let an attacker fill the
database with garbage tokens for free.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

# Redemption ``code`` values that can never succeed on retry. A token that was
# already spent, failed to decode, or redeemed to zero value stays that way
# forever; swap fees exceeding the token amount only changes if the mint
# lowers its fees, which the TTL covers.
TERMINAL_REDEMPTION_CODES: frozenset[str] = frozenset(
    {
        "cashu_token_already_spent",
        "invalid_cashu_token",
        "cashu_token_zero_value",
        "cashu_token_swap_fees_exceed_amount",
    }
)

DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class CachedRedemptionFailure:
    """Sanitized classification of a terminal redemption failure.

    Mirrors the ``(type, status, message, code)`` tuple produced by
    ``classify_redemption_error`` so a cache hit yields a byte-identical error
    envelope to the original mint-backed failure.
    """

    status_code: int
    error_type: str
    message: str
    code: str


class RedemptionNegativeCache:
    """Bounded TTL+LRU cache keyed by the SHA-256 hash of the bearer token.

    Not thread-safe by design: all access happens on the asyncio event loop.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, CachedRedemptionFailure]] = (
            OrderedDict()
        )

    def get(self, hashed_key: str) -> CachedRedemptionFailure | None:
        entry = self._entries.get(hashed_key)
        if entry is None:
            return None
        expires_at, failure = entry
        if self._clock() >= expires_at:
            del self._entries[hashed_key]
            return None
        self._entries.move_to_end(hashed_key)
        return failure

    def put(self, hashed_key: str, failure: CachedRedemptionFailure) -> None:
        if hashed_key in self._entries:
            del self._entries[hashed_key]
        elif len(self._entries) >= self._max_entries:
            self._entries.popitem(last=False)
        self._entries[hashed_key] = (self._clock() + self._ttl_seconds, failure)

    def discard(self, hashed_key: str) -> None:
        self._entries.pop(hashed_key, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


# Process-wide singleton used by the bearer-auth path.
redemption_negative_cache = RedemptionNegativeCache()
