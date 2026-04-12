"""Dynamic provider fee schedule logic.

Supports time-based fee ranges (HH:MM UTC) with overlap validation and active fee resolution.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic.v1 import BaseModel, validator

_HH_MM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class FeeTimeRange(BaseModel):
    start_time: str  # HH:MM UTC
    end_time: str  # HH:MM UTC
    provider_fee: float

    @validator("start_time", "end_time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        if not _HH_MM_RE.match(v):
            raise ValueError(f"Time must be in HH:MM format (00:00–23:59), got: {v!r}")
        return v

    @validator("provider_fee")
    @classmethod
    def validate_fee(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"provider_fee must be > 0 (got {v})")
        return v


def _to_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _range_intervals(r: FeeTimeRange) -> list[tuple[int, int]]:
    """Return list of [start, end) minute intervals for this range.

    Handles midnight-crossing (e.g. 22:00–06:00 → [(1320,1440),(0,360)]).
    start == end is treated as a full-day range.
    """
    start = _to_minutes(r.start_time)
    end = _to_minutes(r.end_time)
    if start < end:
        return [(start, end)]
    if start > end:
        return [(start, 1440), (0, end)]
    # start == end → full day
    return [(0, 1440)]


def _intervals_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def ranges_overlap(a: FeeTimeRange, b: FeeTimeRange) -> bool:
    """Return True if two fee time ranges overlap at any point in the day."""
    for ia in _range_intervals(a):
        for ib in _range_intervals(b):
            if _intervals_overlap(ia, ib):
                return True
    return False


def validate_no_overlaps(ranges: list[FeeTimeRange]) -> None:
    """Raise ValueError if any two ranges in the list overlap."""
    for i in range(len(ranges)):
        for j in range(i + 1, len(ranges)):
            if ranges_overlap(ranges[i], ranges[j]):
                raise ValueError(
                    f"Fee ranges overlap: [{ranges[i].start_time}–{ranges[i].end_time}]"
                    f" and [{ranges[j].start_time}–{ranges[j].end_time}]"
                )


def get_active_fee(
    ranges: list[FeeTimeRange] | None,
    default_fee: float,
    *,
    _now: datetime | None = None,
) -> float:
    """Return the provider fee for the current UTC time.

    Falls back to *default_fee* when no range matches or *ranges* is empty/None.
    The *_now* parameter is for testing only.
    """
    if not ranges or not isinstance(ranges, list):
        return default_fee

    now = _now if _now is not None else datetime.now(timezone.utc)
    # Normalize to UTC
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    current = now.hour * 60 + now.minute

    for r in ranges:
        start = _to_minutes(r.start_time)
        end = _to_minutes(r.end_time)
        if start < end:
            if start <= current < end:
                return r.provider_fee
        elif start > end:  # midnight-crossing
            if current >= start or current < end:
                return r.provider_fee
        else:  # full day (start == end)
            return r.provider_fee

    return default_fee
