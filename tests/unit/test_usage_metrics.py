from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from routstr.core.usage_metrics import UsageMetricsService


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


@pytest.mark.asyncio
async def test_usage_metrics_counts(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    now = datetime(2025, 1, 1, 12, 0, 0)
    records = [
        {
            "asctime": (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
            "levelname": "ERROR",
            "message": "Proxy failure",
        },
        {
            "asctime": (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
            "levelname": "INFO",
            "message": "Received upstream response",
            "status_code": 200,
            "path": "chat/completions",
        },
        {
            "asctime": (now - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"),
            "levelname": "INFO",
            "message": "Received upstream response",
            "status_code": 500,
            "path": "chat/completions",
        },
    ]
    _write_records(log_dir / "app_2025-01-01.log", records)

    result = await UsageMetricsService.collect(
        metrics=["errors", "chat_completions_success"],
        bucket_minutes=15,
        hours=1,
        log_dir=log_dir,
        now=now,
    )

    errors_series = next(series for series in result.series if series.name == "errors")
    completions_series = next(
        series
        for series in result.series
        if series.name == "chat_completions_success"
    )

    assert errors_series.total == 1
    assert sum(point.count for point in errors_series.points) == 1
    assert completions_series.total == 1
    assert any(point.count == 1 for point in completions_series.points)


@pytest.mark.asyncio
async def test_usage_metrics_invalid_metric(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    now = datetime(2025, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        await UsageMetricsService.collect(
            metrics=["unknown"],
            bucket_minutes=15,
            hours=1,
            log_dir=log_dir,
            now=now,
        )
