from __future__ import annotations

import asyncio
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Literal, cast

UsageMetricName = Literal["errors", "chat_completions_success"]


@dataclass(frozen=True)
class MetricDefinition:
    name: UsageMetricName
    label: str
    description: str
    matcher: Callable[[dict[str, object]], bool]


@dataclass(frozen=True)
class UsageMetricPoint:
    bucket_start: datetime
    count: int


@dataclass(frozen=True)
class UsageMetricSeries:
    name: UsageMetricName
    label: str
    description: str
    total: int
    points: list[UsageMetricPoint]


@dataclass(frozen=True)
class UsageMetricsComputation:
    bucket_minutes: int
    bucket_count: int
    start: datetime
    end: datetime
    series: list[UsageMetricSeries]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _is_error(record: dict[str, object]) -> bool:
    level = record.get("levelname")
    if not isinstance(level, str):
        return False
    return level.upper() in {"ERROR", "CRITICAL"}


def _coerce_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _is_successful_chat_completion(record: dict[str, object]) -> bool:
    message = record.get("message")
    if not isinstance(message, str) or message != "Received upstream response":
        return False
    status_code = _coerce_int(record.get("status_code"))
    if status_code != 200:
        return False
    path = record.get("path")
    return isinstance(path, str) and path.endswith("chat/completions")


METRIC_DEFINITIONS: dict[UsageMetricName, MetricDefinition] = {
    "errors": MetricDefinition(
        name="errors",
        label="Errors",
        description="Log entries emitted at ERROR or CRITICAL level",
        matcher=_is_error,
    ),
    "chat_completions_success": MetricDefinition(
        name="chat_completions_success",
        label="200 chat/completions",
        description="Successful upstream responses for chat/completions",
        matcher=_is_successful_chat_completion,
    ),
}


def list_metric_definitions() -> list[dict[str, str]]:
    return [
        {
            "name": definition.name,
            "label": definition.label,
            "description": definition.description,
        }
        for definition in METRIC_DEFINITIONS.values()
    ]


def default_metric_names() -> list[UsageMetricName]:
    return list(METRIC_DEFINITIONS.keys())


class UsageMetricsService:
    @classmethod
    async def collect(
        cls,
        metrics: list[str],
        bucket_minutes: int,
        hours: int,
        log_dir: Path | None = None,
        now: datetime | None = None,
    ) -> UsageMetricsComputation:
        return await asyncio.to_thread(
            cls._collect_sync, metrics, bucket_minutes, hours, log_dir, now
        )

    @classmethod
    def _collect_sync(
        cls,
        metrics: list[str],
        bucket_minutes: int,
        hours: int,
        log_dir: Path | None,
        now: datetime | None,
    ) -> UsageMetricsComputation:
        metric_list = cls._sanitize_metrics(metrics)
        bucket_minutes = cls._clamp(bucket_minutes, minimum=1, maximum=24 * 60)
        hours = cls._clamp(hours, minimum=1, maximum=24 * 7)
        now_dt = now or datetime.now()
        start = now_dt - timedelta(hours=hours)
        bucket_seconds = bucket_minutes * 60
        bucket_count = max(1, math.ceil((hours * 3600) / bucket_seconds))
        bucket_starts = [
            start + timedelta(seconds=bucket_seconds * index)
            for index in range(bucket_count)
        ]
        counts: dict[UsageMetricName, list[int]] = {
            name: [0] * bucket_count for name in metric_list
        }

        for log_file in cls._iter_log_files(log_dir):
            try:
                with log_file.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        record = cls._parse_record(line)
                        if not record:
                            continue
                        timestamp = cls._parse_timestamp(record.get("asctime"))
                        if timestamp is None or timestamp < start or timestamp > now_dt:
                            continue
                        bucket_index = cls._bucket_index(
                            timestamp, start, bucket_seconds, bucket_count
                        )
                        if bucket_index is None:
                            continue
                        for metric_name in metric_list:
                            definition = METRIC_DEFINITIONS[metric_name]
                            if definition.matcher(record):
                                counts[metric_name][bucket_index] += 1
            except (OSError, UnicodeDecodeError):
                continue

        series = [
            UsageMetricSeries(
                name=metric_name,
                label=METRIC_DEFINITIONS[metric_name].label,
                description=METRIC_DEFINITIONS[metric_name].description,
                total=sum(counts[metric_name]),
                points=[
                    UsageMetricPoint(bucket_start=bucket_starts[index], count=count)
                    for index, count in enumerate(counts[metric_name])
                ],
            )
            for metric_name in metric_list
        ]

        return UsageMetricsComputation(
            bucket_minutes=bucket_minutes,
            bucket_count=bucket_count,
            start=start,
            end=now_dt,
            series=series,
        )

    @staticmethod
    def _sanitize_metrics(metrics: list[str]) -> list[UsageMetricName]:
        requested = [metric.strip() for metric in metrics if metric.strip()]
        unique: list[UsageMetricName] = []
        for normalized in requested:
            if normalized not in METRIC_DEFINITIONS:
                continue
            metric_name = cast(UsageMetricName, normalized)
            if metric_name not in unique:
                unique.append(metric_name)
        if unique:
            return unique
        if requested:
            raise ValueError("No valid usage metrics requested")
        return default_metric_names()

    @staticmethod
    def _clamp(value: int, *, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, value))

    @staticmethod
    def _iter_log_files(log_dir: Path | None) -> list[Path]:
        directory = log_dir or Path("logs")
        if not directory.exists():
            return []
        log_files = sorted(
            directory.glob("*.log"),
            key=UsageMetricsService._safe_mtime,
            reverse=True,
        )
        return log_files

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    @staticmethod
    def _parse_record(line: str) -> dict[str, object] | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @staticmethod
    def _bucket_index(
        timestamp: datetime,
        start: datetime,
        bucket_seconds: int,
        bucket_count: int,
    ) -> int | None:
        delta_seconds = (timestamp - start).total_seconds()
        if delta_seconds < 0:
            return None
        index = int(delta_seconds // bucket_seconds)
        if index >= bucket_count:
            index = bucket_count - 1
        return index

