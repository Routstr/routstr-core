import time
from pathlib import Path

import pytest

from routstr.core.usage_analytics_store import UsageAnalyticsStore


def test_local_log_minutes_are_bucketed_in_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        store = UsageAnalyticsStore(logs_dir=tmp_path)
        conn = store._get_connection_locked()
        conn.execute(
            "INSERT INTO analytics_minute (minute_ts, total_requests) VALUES (?, 1)",
            ("2026-09-18 16:30:00",),
        )
        metrics = store._query_metrics_locked(
            conn,
            cutoff_timestamp="2026-09-18 00:00:00",
            interval_minutes=60,
            hours_back=24,
        )
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()

    # 16:30 IST is 11:00 UTC; rounding the local hour first would give 10:00.
    assert [point["timestamp"] for point in metrics["metrics"]] == [
        "2026-09-18 11:00:00"
    ]


@pytest.mark.parametrize("indexed", [True, False])
def test_error_timestamp_utc_matches_buckets_without_changing_legacy_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, indexed: bool
) -> None:
    import json
    import os
    from datetime import datetime, timezone

    from routstr.core.log_manager import LogManager

    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        (tmp_path / f"app_{stamp[:10]}.log").write_text(
            json.dumps({"asctime": stamp, "levelname": "ERROR", "message": "fixture"})
            + "\n"
        )
        manager = LogManager(tmp_path)
        if not indexed:

            def fail_index(**kwargs: object) -> dict:
                raise RuntimeError("exercise raw-log fallback")

            monkeypatch.setattr(manager._usage_store, "get_error_details", fail_index)
            monkeypatch.setattr(manager._usage_store, "get_dashboard", fail_index)
        expected = (
            datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        standalone = manager.get_error_details(hours=24)["errors"][0]
        dashboard = manager.get_usage_dashboard(hours=24)["error_details"]["errors"][0]
        for error in (standalone, dashboard):
            assert error["timestamp"] == stamp
            assert error["timestamp_utc"] == expected
    finally:
        if old_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old_tz)
        time.tzset()


def test_malformed_local_timestamp_has_no_invented_utc_value() -> None:
    from routstr.core.usage_analytics_store import local_log_timestamp_utc

    assert local_log_timestamp_utc("invalid") is None
