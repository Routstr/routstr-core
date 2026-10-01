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
