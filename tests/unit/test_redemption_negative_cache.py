"""Unit tests for the redemption failure cache."""

from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from routstr.auth import (
    _cached_failure_to_http_exception,
    _maybe_cache_redemption_failure,
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
TRANSIENT_FAILURE = CachedRedemptionFailure(
    status_code=503,
    error_type="mint_unreachable",
    message="Cashu mint unavailable",
    code="cashu_mint_unreachable",
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

    def test_file_tier_is_visible_to_another_worker(self, tmp_path: Path) -> None:
        key = "a" * 64
        first = RedemptionNegativeCache(storage_dir=tmp_path)
        second = RedemptionNegativeCache(storage_dir=tmp_path)

        first.put(key, FAILURE)

        assert second.get(key) == FAILURE
        assert (tmp_path / key).stat().st_mode & 0o777 == 0o600

    def test_file_tier_expires_and_removes_entry(self, tmp_path: Path) -> None:
        clock = FakeClock()
        key = "b" * 64
        first = RedemptionNegativeCache(
            storage_dir=tmp_path, clock=clock, wall_clock=clock
        )
        second = RedemptionNegativeCache(
            storage_dir=tmp_path, clock=clock, wall_clock=clock
        )
        first.put(key, FAILURE, ttl_seconds=10)
        clock.now = 10

        assert second.get(key) is None
        assert not (tmp_path / key).exists()

    def test_corrupt_file_is_ignored_and_removed(self, tmp_path: Path) -> None:
        key = "c" * 64
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / key).write_text("not-json")
        cache = RedemptionNegativeCache(storage_dir=tmp_path)

        assert cache.get(key) is None
        assert not (tmp_path / key).exists()

    def test_transient_ttl_is_clamped_to_contract_maximum(self) -> None:
        clock = FakeClock()
        cache = RedemptionNegativeCache(clock=clock, wall_clock=clock)
        cache.put("deadbeef", TRANSIENT_FAILURE, ttl_seconds=3_600)

        clock.now = 60
        assert cache.get("deadbeef") is None

    def test_shared_transient_ttl_ignores_backward_wall_step(
        self, tmp_path: Path
    ) -> None:
        monotonic = FakeClock()
        monotonic.now = 100
        wall = FakeClock()
        wall.now = 10_000
        key = "d" * 64
        first = RedemptionNegativeCache(
            storage_dir=tmp_path, clock=monotonic, wall_clock=wall
        )
        first.put(key, TRANSIENT_FAILURE, ttl_seconds=30)

        wall.now = 6_400
        monotonic.now = 110
        second = RedemptionNegativeCache(
            storage_dir=tmp_path, clock=monotonic, wall_clock=wall
        )
        assert second.get(key) == TRANSIENT_FAILURE
        monotonic.now = 130
        assert second.get(key) is None

    def test_legacy_transient_ttl_is_bounded_after_backward_wall_step(
        self, tmp_path: Path
    ) -> None:
        from routstr import node_coordination

        clock = FakeClock()
        clock.now = 6_400
        key = "e" * 64
        node_coordination.write_json(
            tmp_path / key,
            {
                "version": 1,
                "expires_at": 10_030,
                "status_code": TRANSIENT_FAILURE.status_code,
                "error_type": TRANSIENT_FAILURE.error_type,
                "message": TRANSIENT_FAILURE.message,
                "code": TRANSIENT_FAILURE.code,
            },
        )
        cache = RedemptionNegativeCache(
            storage_dir=tmp_path, clock=clock, wall_clock=clock
        )

        with patch("routstr.node_coordination.NODE_BOOT_ID", "boot-a"):
            assert cache.get(key) == TRANSIENT_FAILURE
            clock.now += 61
            assert cache.get(key) is None

    @pytest.mark.parametrize(
        "field,value",
        [("expires_at", float("nan")), ("monotonic_until", float("inf"))],
    )
    def test_shared_non_finite_deadline_is_rejected(
        self, tmp_path: Path, field: str, value: float
    ) -> None:
        from routstr import node_coordination

        key = "f" * 64
        state: dict[str, object] = {
            "version": 2,
            "boot_id": node_coordination.NODE_BOOT_ID,
            "monotonic_until": 30.0,
            "expires_at": 30.0,
            "ttl_seconds": 30.0,
            "status_code": TRANSIENT_FAILURE.status_code,
            "error_type": TRANSIENT_FAILURE.error_type,
            "message": TRANSIENT_FAILURE.message,
            "code": TRANSIENT_FAILURE.code,
        }
        state[field] = value
        node_coordination.write_json(tmp_path / key, state)

        assert RedemptionNegativeCache(storage_dir=tmp_path).get(key) is None
        assert not (tmp_path / key).exists()

    def test_shared_write_failure_keeps_memory_entry(self, tmp_path: Path) -> None:
        key = "d" * 64
        cache = RedemptionNegativeCache(storage_dir=tmp_path)

        with patch(
            "routstr.redemption_cache.node_coordination.write_json",
            side_effect=OSError("disk unavailable"),
        ):
            cache.put(key, FAILURE)

        assert cache.get(key) == FAILURE

    def test_trim_ignores_file_removed_during_stat(self, tmp_path: Path) -> None:
        cache = RedemptionNegativeCache(max_entries=1, storage_dir=tmp_path)
        missing = MagicMock()
        missing.is_file.return_value = True
        missing.stat.side_effect = FileNotFoundError
        present = MagicMock()
        present.is_file.return_value = True
        present.stat.return_value.st_mtime = 1.0

        with patch(
            "routstr.redemption_cache.os.scandir", return_value=[missing, present]
        ):
            cache._trim_shared()


class TestMaybeCacheRedemptionFailure:
    def test_already_spent_error_is_cached(self) -> None:
        _maybe_cache_redemption_failure(
            "deadbeef", Exception("Mint Error: Token already spent. (Code: 11001)")
        )
        cached = redemption_negative_cache.get("deadbeef")
        assert cached is not None
        assert cached.code == "cashu_token_already_spent"
        assert cached.status_code == 400

    def test_transient_mint_unreachable_is_cached_briefly(self) -> None:
        import httpx

        _maybe_cache_redemption_failure(
            "deadbeef", httpx.ConnectError("connection refused")
        )
        cached = redemption_negative_cache.get("deadbeef")
        assert cached is not None
        assert cached.code == "cashu_mint_unreachable"
        assert cached.status_code == 503

    def test_unclassified_error_is_not_cached(self) -> None:
        _maybe_cache_redemption_failure("deadbeef", RuntimeError("some internal fault"))
        assert redemption_negative_cache.get("deadbeef") is None

    def test_generic_value_error_is_not_cached(self) -> None:
        # The generic code is deliberately not cached because a ValueError may
        # wrap a transient fault.
        _maybe_cache_redemption_failure(
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
