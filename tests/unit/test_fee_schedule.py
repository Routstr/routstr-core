"""Unit tests for routstr.payment.fee_schedule."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic.v1 import ValidationError

from routstr.payment.fee_schedule import (
    FeeTimeRange,
    get_active_fee,
    ranges_overlap,
    validate_no_overlaps,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _r(start: str, end: str, fee: float = 1.05) -> FeeTimeRange:
    return FeeTimeRange(start_time=start, end_time=end, provider_fee=fee)


def _now(h: int, m: int = 0) -> datetime:
    return datetime(2026, 1, 1, h, m, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# FeeTimeRange validation
# ---------------------------------------------------------------------------


class TestFeeTimeRangeValidation:
    def test_valid_range(self) -> None:
        r = _r("08:00", "18:00", 1.05)
        assert r.start_time == "08:00"
        assert r.end_time == "18:00"
        assert r.provider_fee == 1.05

    def test_invalid_start_time_format(self) -> None:
        with pytest.raises(ValidationError, match="HH:MM"):
            _r("8:00", "18:00")

    def test_invalid_end_time_hour_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            _r("08:00", "24:00")

    def test_invalid_end_time_minute_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            _r("08:00", "18:60")

    def test_invalid_time_letters(self) -> None:
        with pytest.raises(ValidationError):
            _r("ab:cd", "18:00")

    def test_fee_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="provider_fee must be > 0"):
            _r("08:00", "18:00", fee=0.0)

    def test_fee_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _r("08:00", "18:00", fee=-0.5)

    def test_fee_below_one_allowed(self) -> None:
        r = _r("08:00", "18:00", fee=0.95)
        assert r.provider_fee == 0.95

    def test_boundary_times_valid(self) -> None:
        r = _r("00:00", "23:59")
        assert r.start_time == "00:00"
        assert r.end_time == "23:59"


# ---------------------------------------------------------------------------
# ranges_overlap
# ---------------------------------------------------------------------------


class TestRangesOverlap:
    def test_non_overlapping_ranges(self) -> None:
        assert not ranges_overlap(_r("08:00", "12:00"), _r("12:00", "18:00"))

    def test_overlapping_ranges(self) -> None:
        assert ranges_overlap(_r("08:00", "14:00"), _r("12:00", "18:00"))

    def test_one_contains_the_other(self) -> None:
        assert ranges_overlap(_r("08:00", "20:00"), _r("10:00", "18:00"))

    def test_identical_ranges_overlap(self) -> None:
        assert ranges_overlap(_r("08:00", "12:00"), _r("08:00", "12:00"))

    def test_adjacent_non_overlapping(self) -> None:
        # end of first == start of second → no overlap (open interval [start, end))
        assert not ranges_overlap(_r("06:00", "12:00"), _r("12:00", "18:00"))

    def test_midnight_crossing_vs_day_range_overlap(self) -> None:
        # 22:00–06:00 crosses midnight; 04:00–08:00 should overlap (both cover 04:00–06:00)
        assert ranges_overlap(_r("22:00", "06:00"), _r("04:00", "08:00"))

    def test_midnight_crossing_vs_non_overlapping_day_range(self) -> None:
        # 22:00–06:00 does NOT cover 10:00–18:00
        assert not ranges_overlap(_r("22:00", "06:00"), _r("10:00", "18:00"))

    def test_two_midnight_crossing_ranges_overlap(self) -> None:
        assert ranges_overlap(_r("20:00", "04:00"), _r("22:00", "06:00"))

    def test_two_midnight_crossing_ranges_non_overlap(self) -> None:
        # 21:00–23:00 and 23:00–21:00 (full day minus one hour): they do overlap
        # Let's use a case that genuinely doesn't: 21:00–22:00 adjacent
        # Actually for two midnight-crossing ranges it's hard to not overlap—let's test equal endpoints
        assert not ranges_overlap(_r("22:00", "23:00"), _r("23:00", "01:00"))


# ---------------------------------------------------------------------------
# validate_no_overlaps
# ---------------------------------------------------------------------------


class TestValidateNoOverlaps:
    def test_no_overlaps_passes(self) -> None:
        validate_no_overlaps(
            [_r("00:00", "08:00"), _r("08:00", "16:00"), _r("16:00", "23:59")]
        )

    def test_overlap_raises(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            validate_no_overlaps([_r("08:00", "14:00"), _r("12:00", "18:00")])

    def test_single_range_passes(self) -> None:
        validate_no_overlaps([_r("08:00", "18:00")])

    def test_empty_list_passes(self) -> None:
        validate_no_overlaps([])

    def test_midnight_crossing_overlap_detected(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            validate_no_overlaps([_r("22:00", "06:00"), _r("04:00", "08:00")])


# ---------------------------------------------------------------------------
# get_active_fee
# ---------------------------------------------------------------------------


class TestGetActiveFee:
    def test_returns_default_when_no_ranges(self) -> None:
        assert get_active_fee(None, 1.01) == 1.01

    def test_returns_default_for_empty_list(self) -> None:
        assert get_active_fee([], 1.01) == 1.01

    def test_returns_matching_fee(self) -> None:
        ranges = [_r("08:00", "18:00", fee=1.05)]
        assert get_active_fee(ranges, 1.01, _now=_now(12)) == 1.05

    def test_returns_default_when_no_match(self) -> None:
        ranges = [_r("08:00", "18:00", fee=1.05)]
        assert get_active_fee(ranges, 1.01, _now=_now(20)) == 1.01

    def test_boundary_start_inclusive(self) -> None:
        ranges = [_r("08:00", "18:00", fee=1.05)]
        assert get_active_fee(ranges, 1.01, _now=_now(8, 0)) == 1.05

    def test_boundary_end_exclusive(self) -> None:
        ranges = [_r("08:00", "18:00", fee=1.05)]
        assert get_active_fee(ranges, 1.01, _now=_now(18, 0)) == 1.01

    def test_midnight_crossing_before_midnight(self) -> None:
        ranges = [_r("22:00", "06:00", fee=1.03)]
        assert get_active_fee(ranges, 1.01, _now=_now(23)) == 1.03

    def test_midnight_crossing_after_midnight(self) -> None:
        ranges = [_r("22:00", "06:00", fee=1.03)]
        assert get_active_fee(ranges, 1.01, _now=_now(3)) == 1.03

    def test_midnight_crossing_outside_range(self) -> None:
        ranges = [_r("22:00", "06:00", fee=1.03)]
        assert get_active_fee(ranges, 1.01, _now=_now(12)) == 1.01

    def test_multiple_ranges_correct_match(self) -> None:
        ranges = [
            _r("00:00", "08:00", fee=1.02),
            _r("08:00", "16:00", fee=1.05),
            _r("16:00", "23:59", fee=1.03),
        ]
        assert get_active_fee(ranges, 1.01, _now=_now(10)) == 1.05
        assert get_active_fee(ranges, 1.01, _now=_now(2)) == 1.02
        assert get_active_fee(ranges, 1.01, _now=_now(20)) == 1.03

    def test_first_matching_range_wins(self) -> None:
        # When multiple ranges could match (should not happen if validated),
        # the first one wins.
        ranges = [_r("08:00", "20:00", fee=1.05), _r("10:00", "12:00", fee=1.02)]
        assert get_active_fee(ranges, 1.01, _now=_now(11)) == 1.05


# ---------------------------------------------------------------------------
# Timezone-aware inputs (CEST / CET)
# ---------------------------------------------------------------------------


class TestGetActiveFeeTimezones:
    """Verify that tz-aware datetimes are normalised to UTC before matching."""

    # CEST = UTC+2 (Central European Summer Time, used ~late March – late Oct)
    CEST = timezone(timedelta(hours=2))
    # CET = UTC+1 (Central European Time, used the rest of the year)
    CET = timezone(timedelta(hours=1))

    def test_cest_datetime_normalised_to_utc_matches(self) -> None:
        # 10:00 CEST == 08:00 UTC — schedule 08:00–18:00 should match
        now_cest = datetime(2026, 7, 1, 10, 0, tzinfo=self.CEST)
        ranges = [_r("08:00", "18:00", fee=1.05)]
        assert get_active_fee(ranges, 1.01, _now=now_cest) == 1.05

    def test_cest_datetime_normalised_to_utc_no_match(self) -> None:
        # 06:00 CEST == 04:00 UTC — schedule 08:00–18:00 should NOT match
        now_cest = datetime(2026, 7, 1, 6, 0, tzinfo=self.CEST)
        ranges = [_r("08:00", "18:00", fee=1.05)]
        assert get_active_fee(ranges, 1.01, _now=now_cest) == 1.01

    def test_cet_datetime_normalised_to_utc_matches(self) -> None:
        # 09:00 CET == 08:00 UTC — schedule 08:00–18:00 should match
        now_cet = datetime(2026, 1, 15, 9, 0, tzinfo=self.CET)
        ranges = [_r("08:00", "18:00", fee=1.05)]
        assert get_active_fee(ranges, 1.01, _now=now_cet) == 1.05

    def test_cet_datetime_before_utc_range(self) -> None:
        # 08:30 CET == 07:30 UTC — schedule 08:00–18:00 should NOT match
        now_cet = datetime(2026, 1, 15, 8, 30, tzinfo=self.CET)
        ranges = [_r("08:00", "18:00", fee=1.05)]
        assert get_active_fee(ranges, 1.01, _now=now_cet) == 1.01

    def test_cest_midnight_crossing_before_midnight(self) -> None:
        # 00:30 CEST == 22:30 UTC — schedule 22:00–06:00 UTC should match
        now_cest = datetime(2026, 7, 2, 0, 30, tzinfo=self.CEST)
        ranges = [_r("22:00", "06:00", fee=1.03)]
        assert get_active_fee(ranges, 1.01, _now=now_cest) == 1.03

    def test_cest_midnight_crossing_after_midnight(self) -> None:
        # 05:00 CEST == 03:00 UTC — schedule 22:00–06:00 UTC should match
        now_cest = datetime(2026, 7, 2, 5, 0, tzinfo=self.CEST)
        ranges = [_r("22:00", "06:00", fee=1.03)]
        assert get_active_fee(ranges, 1.01, _now=now_cest) == 1.03

    def test_cest_midnight_crossing_outside_range(self) -> None:
        # 14:00 CEST == 12:00 UTC — schedule 22:00–06:00 UTC should NOT match
        now_cest = datetime(2026, 7, 2, 14, 0, tzinfo=self.CEST)
        ranges = [_r("22:00", "06:00", fee=1.03)]
        assert get_active_fee(ranges, 1.01, _now=now_cest) == 1.01

    def test_naive_utc_datetime_still_works(self) -> None:
        # Naive datetimes are treated as UTC (defensive fallback path)
        now_naive = datetime(2026, 1, 1, 12, 0)  # no tzinfo
        ranges = [_r("08:00", "18:00", fee=1.05)]
        assert get_active_fee(ranges, 1.01, _now=now_naive) == 1.05
