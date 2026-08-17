"""Unit tests for the terminal-redemption negative cache."""

from typing import Iterator

import pytest
from fastapi import HTTPException

from routstr.auth import (
    _cached_failure_to_http_exception,
    _maybe_cache_terminal_redemption_failure,
)
from routstr.redemption_cache import (
    TERMINAL_REDEMPTION_CODES,
    CachedRedemptionFailure,
    RedemptionNegativeCache,
    redemption_negative_cache,
)

FAILURE = CachedRedemptionFailure(
    status_code=400,
    error_type="token_already_spent",
    message="Cashu token already spent",
    code="cashu_token_already_spent",
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean_singleton() -> Iterator[None]:
    redemption_negative_cache.clear()
    yield
    redemption_negative_cache.clear()


class TestRedemptionNegativeCache:
    def test_get_returns_none_for_unknown_key(self) -> None:
        cache = RedemptionNegativeCache()
        assert cache.get("deadbeef") is None

    def test_put_then_get_roundtrip(self) -> None:
        cache = RedemptionNegativeCache()
        cache.put("deadbeef", FAILURE)
        assert cache.get("deadbeef") == FAILURE

    def test_entry_expires_after_ttl(self) -> None:
        clock = FakeClock()
        cache = RedemptionNegativeCache(ttl_seconds=100, clock=clock)
        cache.put("deadbeef", FAILURE)
        clock.now = 99.9
        assert cache.get("deadbeef") == FAILURE
        clock.now = 100.0
        assert cache.get("deadbeef") is None
        assert len(cache) == 0

    def test_lru_eviction_at_capacity(self) -> None:
        cache = RedemptionNegativeCache(max_entries=2)
        cache.put("a", FAILURE)
        cache.put("b", FAILURE)
        # Touch "a" so "b" becomes the least recently used entry.
        assert cache.get("a") is not None
        cache.put("c", FAILURE)
        assert cache.get("b") is None
        assert cache.get("a") is not None
        assert cache.get("c") is not None

    def test_reput_refreshes_expiry(self) -> None:
        clock = FakeClock()
        cache = RedemptionNegativeCache(ttl_seconds=100, clock=clock)
        cache.put("deadbeef", FAILURE)
        clock.now = 90.0
        cache.put("deadbeef", FAILURE)
        clock.now = 150.0
        assert cache.get("deadbeef") == FAILURE

    def test_discard_removes_entry(self) -> None:
        cache = RedemptionNegativeCache()
        cache.put("deadbeef", FAILURE)
        cache.discard("deadbeef")
        assert cache.get("deadbeef") is None
        cache.discard("deadbeef")  # idempotent

    def test_invalid_construction_args_rejected(self) -> None:
        with pytest.raises(ValueError):
            RedemptionNegativeCache(max_entries=0)
        with pytest.raises(ValueError):
            RedemptionNegativeCache(ttl_seconds=0)


class TestMaybeCacheTerminalRedemptionFailure:
    def test_already_spent_error_is_cached(self) -> None:
        _maybe_cache_terminal_redemption_failure(
            "deadbeef", Exception("Mint Error: Token already spent. (Code: 11001)")
        )
        cached = redemption_negative_cache.get("deadbeef")
        assert cached is not None
        assert cached.code == "cashu_token_already_spent"
        assert cached.status_code == 400

    def test_transient_mint_unreachable_is_not_cached(self) -> None:
        import httpx

        _maybe_cache_terminal_redemption_failure(
            "deadbeef", httpx.ConnectError("connection refused")
        )
        assert redemption_negative_cache.get("deadbeef") is None

    def test_unclassified_error_is_not_cached(self) -> None:
        _maybe_cache_terminal_redemption_failure(
            "deadbeef", RuntimeError("some internal fault")
        )
        assert redemption_negative_cache.get("deadbeef") is None

    def test_generic_value_error_is_not_cached(self) -> None:
        # cashu_token_redemption_failed is deliberately NOT terminal — a
        # generic ValueError can wrap transient faults.
        _maybe_cache_terminal_redemption_failure(
            "deadbeef", ValueError("something went wrong during redemption")
        )
        assert redemption_negative_cache.get("deadbeef") is None

    def test_terminal_codes_are_a_closed_set(self) -> None:
        assert TERMINAL_REDEMPTION_CODES == {
            "cashu_token_already_spent",
            "invalid_cashu_token",
            "cashu_token_zero_value",
            "cashu_token_swap_fees_exceed_amount",
        }


class TestCachedFailureToHttpException:
    def test_envelope_matches_classify_taxonomy(self) -> None:
        exc = _cached_failure_to_http_exception(FAILURE)
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 400
        assert exc.detail == {
            "error": {
                "message": "Cashu token already spent",
                "type": "token_already_spent",
                "code": "cashu_token_already_spent",
            }
        }
