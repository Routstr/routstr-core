from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

LOG_FILE_PREFIX = "app_"
LOG_FILE_SUFFIX = ".log"
LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_SEARCH_FILE_LIMIT = 7

LogEntry = dict[str, Any]


@dataclass(slots=True)
class LogSearchFilters:
    date: str | None = None
    level: str | None = None
    request_id: str | None = None
    search_text: str | None = None


@dataclass(slots=True)
class EntryInsights:
    timestamp: datetime | None
    level: str
    model: str | None
    event_total_request: bool
    event_success: bool
    event_fail: bool
    event_payment_processed: bool
    event_upstream_error: bool
    revenue_msats: float
    refund_msats: float
    error_type: str | None


def search_logs(
    logs_dir: Path, filters: LogSearchFilters, limit: int = 100
) -> list[LogEntry]:
    if limit <= 0 or not logs_dir.exists():
        return []

    files = _collect_log_files(
        logs_dir,
        date=filters.date,
        limit=None if filters.date else DEFAULT_SEARCH_FILE_LIMIT,
    )

    search_lower = filters.search_text.lower() if filters.search_text else None
    normalized_level = filters.level.upper() if filters.level else None
    entries: list[LogEntry] = []

    for log_file in files:
        for entry in _stream_log_entries(log_file):
            if not _matches_filters(
                entry=entry,
                level=normalized_level,
                request_id=filters.request_id,
                search_lower=search_lower,
            ):
                continue

            entries.append(entry)
            if len(entries) >= limit:
                break

        if len(entries) >= limit:
            break

    entries.sort(key=lambda e: e.get("asctime", ""), reverse=True)
    return entries[:limit]


def get_available_log_dates(logs_dir: Path, limit: int = 30) -> list[str]:
    if not logs_dir.exists():
        return []

    dates: list[str] = []
    files = _collect_log_files(logs_dir, limit=limit)
    for log_file in files:
        date_str = _extract_date_from_filename(log_file)
        if date_str:
            dates.append(date_str)

    return dates


def usage_metrics(
    logs_dir: Path, interval_minutes: int, hours: int
) -> dict[str, Any]:
    if not logs_dir.exists():
        return {
            "metrics": [],
            "interval_minutes": interval_minutes,
            "hours_back": hours,
            "total_buckets": 0,
        }

    entries = _load_entries_since(logs_dir, hours)
    if not entries:
        return {
            "metrics": [],
            "interval_minutes": interval_minutes,
            "hours_back": hours,
            "total_buckets": 0,
        }

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    buckets: dict[str, dict[str, float]] = {}

    for entry in entries:
        insights = _analyze_entry(entry)
        timestamp = insights.timestamp
        if timestamp is None or timestamp < cutoff:
            continue

        bucket_time = timestamp.replace(
            minute=(timestamp.minute // interval_minutes) * interval_minutes,
            second=0,
            microsecond=0,
        )
        bucket_key = bucket_time.isoformat()
        bucket = buckets.setdefault(
            bucket_key,
            {
                "total_requests": 0,
                "successful_chat_completions": 0,
                "failed_requests": 0,
                "errors": 0,
                "warnings": 0,
                "payment_processed": 0,
                "upstream_errors": 0,
                "revenue_msats": 0.0,
                "refunds_msats": 0.0,
            },
        )

        if insights.level == "ERROR":
            bucket["errors"] += 1
        elif insights.level == "WARNING":
            bucket["warnings"] += 1

        if insights.event_total_request:
            bucket["total_requests"] += 1
        if insights.event_success:
            bucket["successful_chat_completions"] += 1
            bucket["revenue_msats"] += insights.revenue_msats
        if insights.event_fail:
            bucket["failed_requests"] += 1
            bucket["refunds_msats"] += insights.refund_msats
        if insights.event_payment_processed:
            bucket["payment_processed"] += 1
        if insights.event_upstream_error:
            bucket["upstream_errors"] += 1

    metrics = [
        {"timestamp": key, **values}
        for key, values in sorted(buckets.items(), key=lambda item: item[0])
    ]

    return {
        "metrics": metrics,
        "interval_minutes": interval_minutes,
        "hours_back": hours,
        "total_buckets": len(metrics),
    }


def summarize_usage(logs_dir: Path, hours: int) -> dict[str, Any]:
    if not logs_dir.exists():
        return _empty_summary()

    entries = _load_entries_since(logs_dir, hours)
    if not entries:
        return _empty_summary()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    total_entries = 0
    total_requests = 0
    successful = 0
    failed = 0
    total_errors = 0
    total_warnings = 0
    payment_processed = 0
    upstream_errors = 0
    revenue_msats = 0.0
    refunds_msats = 0.0
    unique_models: set[str] = set()
    error_types: dict[str, int] = {}

    for entry in entries:
        insights = _analyze_entry(entry)
        timestamp = insights.timestamp
        if timestamp is None or timestamp < cutoff:
            continue

        total_entries += 1

        if insights.level == "ERROR":
            total_errors += 1
            if insights.error_type:
                error_types[insights.error_type] = (
                    error_types.get(insights.error_type, 0) + 1
                )
        elif insights.level == "WARNING":
            total_warnings += 1

        if insights.event_total_request:
            total_requests += 1
        if insights.event_success:
            successful += 1
            revenue_msats += insights.revenue_msats
        if insights.event_fail:
            failed += 1
            refunds_msats += insights.refund_msats
        if insights.event_payment_processed:
            payment_processed += 1
        if insights.event_upstream_error:
            upstream_errors += 1

        if insights.model:
            unique_models.add(insights.model)

    revenue_sats = revenue_msats / 1000
    refunds_sats = refunds_msats / 1000
    net_revenue_msats = revenue_msats - refunds_msats
    net_revenue_sats = net_revenue_msats / 1000
    success_rate = (successful / total_requests * 100) if total_requests else 0
    refund_rate = (failed / total_requests * 100) if total_requests else 0
    avg_revenue_per_request = (
        revenue_msats / successful if successful else 0
    )

    return {
        "total_entries": total_entries,
        "total_requests": total_requests,
        "successful_chat_completions": successful,
        "failed_requests": failed,
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "payment_processed": payment_processed,
        "upstream_errors": upstream_errors,
        "unique_models_count": len(unique_models),
        "unique_models": sorted(unique_models),
        "error_types": error_types,
        "success_rate": success_rate,
        "revenue_msats": revenue_msats,
        "refunds_msats": refunds_msats,
        "revenue_sats": revenue_sats,
        "refunds_sats": refunds_sats,
        "net_revenue_msats": net_revenue_msats,
        "net_revenue_sats": net_revenue_sats,
        "avg_revenue_per_request_msats": avg_revenue_per_request,
        "refund_rate": refund_rate,
    }


def collect_error_details(
    logs_dir: Path, hours: int, limit: int
) -> dict[str, Any]:
    if not logs_dir.exists():
        return {"errors": [], "total_count": 0}

    entries = _load_entries_since(logs_dir, hours)
    if not entries:
        return {"errors": [], "total_count": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    errors: list[dict[str, Any]] = []

    for entry in entries:
        level = str(entry.get("levelname", "")).upper()
        if level != "ERROR":
            continue

        timestamp = _parse_timestamp(entry)
        if timestamp is None or timestamp < cutoff:
            continue

        timestamp_str = str(entry.get("asctime", ""))
        message = str(entry.get("message", ""))
        error_type = str(entry.get("error_type", "unknown"))
        pathname = str(entry.get("pathname", ""))
        try:
            lineno = int(entry.get("lineno", 0))
        except (TypeError, ValueError):
            lineno = 0
        request_id = str(entry.get("request_id", ""))

        errors.append(
            {
                "timestamp": timestamp_str,
                "message": message,
                "error_type": error_type,
                "pathname": pathname,
                "lineno": lineno,
                "request_id": request_id,
            }
        )

    errors.sort(key=lambda item: item["timestamp"], reverse=True)
    return {"errors": errors[:limit], "total_count": len(errors)}


def revenue_by_model(
    logs_dir: Path, hours: int, limit: int
) -> dict[str, Any]:
    if not logs_dir.exists():
        return {"models": [], "total_revenue_sats": 0, "total_models": 0}

    entries = _load_entries_since(logs_dir, hours)
    if not entries:
        return {"models": [], "total_revenue_sats": 0, "total_models": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    model_stats: dict[str, dict[str, float | int]] = {}

    for entry in entries:
        insights = _analyze_entry(entry)
        timestamp = insights.timestamp
        if timestamp is None or timestamp < cutoff:
            continue

        model = insights.model or "unknown"
        stats = model_stats.setdefault(
            model,
            {
                "requests": 0,
                "successful": 0,
                "failed": 0,
                "revenue_msats": 0.0,
                "refunds_msats": 0.0,
            },
        )

        if insights.event_total_request:
            stats["requests"] += 1
        if insights.event_success:
            stats["successful"] += 1
            stats["revenue_msats"] += insights.revenue_msats
        if insights.event_fail:
            stats["failed"] += 1
            stats["refunds_msats"] += insights.refund_msats

    models: list[dict[str, Any]] = []
    total_revenue_sats = 0.0

    for model, stats in model_stats.items():
        revenue_msats = float(stats["revenue_msats"])
        refunds_msats = float(stats["refunds_msats"])
        requests = int(stats["requests"])
        successful = int(stats["successful"])
        failed = int(stats["failed"])

        revenue_sats = revenue_msats / 1000
        refunds_sats = refunds_msats / 1000
        net_revenue_sats = revenue_sats - refunds_sats
        total_revenue_sats += net_revenue_sats

        avg_revenue = revenue_sats / successful if successful else 0
        models.append(
            {
                "model": model,
                "revenue_sats": revenue_sats,
                "refunds_sats": refunds_sats,
                "net_revenue_sats": net_revenue_sats,
                "requests": requests,
                "successful": successful,
                "failed": failed,
                "avg_revenue_per_request": avg_revenue,
            }
        )

    models.sort(key=lambda item: item["net_revenue_sats"], reverse=True)

    return {
        "models": models[:limit],
        "total_revenue_sats": total_revenue_sats,
        "total_models": len(models),
    }


def _collect_log_files(
    logs_dir: Path, date: str | None = None, limit: int | None = None
) -> list[Path]:
    if date:
        candidate = logs_dir / f"{LOG_FILE_PREFIX}{date}{LOG_FILE_SUFFIX}"
        return [candidate] if candidate.exists() else []

    files = sorted(
        logs_dir.glob(f"{LOG_FILE_PREFIX}*{LOG_FILE_SUFFIX}"),
        key=lambda path: path.name,
        reverse=True,
    )
    if limit is not None:
        return files[:limit]
    return files


def _stream_log_entries(log_file: Path) -> Iterator[LogEntry]:
    try:
        with log_file.open("r") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    if isinstance(data, dict):
                        yield data
                except json.JSONDecodeError:
                    continue
    except Exception:
        return


def _matches_filters(
    entry: LogEntry,
    level: str | None,
    request_id: str | None,
    search_lower: str | None,
) -> bool:
    if level and str(entry.get("levelname", "")).upper() != level:
        return False

    if request_id and entry.get("request_id") != request_id:
        return False

    if search_lower:
        message = str(entry.get("message", "")).lower()
        name = str(entry.get("name", "")).lower()
        if search_lower not in message and search_lower not in name:
            return False

    return True


def _extract_date_from_filename(log_file: Path) -> str | None:
    name = log_file.stem
    if not name.startswith(LOG_FILE_PREFIX):
        return None
    return name.replace(LOG_FILE_PREFIX, "", 1)


def _parse_timestamp(entry: LogEntry) -> datetime | None:
    raw = entry.get("asctime")
    if not raw:
        return None
    try:
        parsed = datetime.strptime(str(raw), LOG_TIME_FORMAT)
        return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _load_entries_since(logs_dir: Path, hours: int) -> list[LogEntry]:
    files = _collect_log_files(logs_dir)
    if not files:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_floor = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    entries: list[LogEntry] = []

    for log_file in files:
        file_date = _extract_date_from_filename(log_file)
        if file_date:
            try:
                parsed_date = datetime.strptime(file_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                if parsed_date < cutoff_floor:
                    break
            except ValueError:
                pass

        entries.extend(_stream_log_entries(log_file))

    return entries


def _analyze_entry(entry: LogEntry) -> EntryInsights:
    message_raw = str(entry.get("message", ""))
    message = message_raw.lower()
    level = str(entry.get("levelname", "")).upper()
    timestamp = _parse_timestamp(entry)
    model_value = entry.get("model")
    model = model_value if isinstance(model_value, str) else None
    error_type = entry.get("error_type")
    error_type_str = str(error_type) if error_type else None

    event_total_request = "received proxy request" in message
    event_success = "token adjustment completed" in message
    event_fail = (
        "upstream request failed" in message or "revert payment" in message
    )
    event_payment_processed = "payment processed successfully" in message
    event_upstream_error = level == "ERROR" and "upstream" in message

    revenue_msats = 0.0
    if event_success:
        cost_data = entry.get("cost_data")
        if isinstance(cost_data, dict):
            total_msats = cost_data.get("total_msats")
            if isinstance(total_msats, (int, float)):
                revenue_msats = float(total_msats)

    refund_msats = 0.0
    if "revert payment" in message:
        max_cost = entry.get("max_cost_for_model")
        if isinstance(max_cost, (int, float)):
            refund_msats = float(max_cost)

    return EntryInsights(
        timestamp=timestamp,
        level=level,
        model=model,
        event_total_request=event_total_request,
        event_success=event_success,
        event_fail=event_fail,
        event_payment_processed=event_payment_processed,
        event_upstream_error=event_upstream_error,
        revenue_msats=revenue_msats,
        refund_msats=refund_msats,
        error_type=error_type_str,
    )


def _empty_summary() -> dict[str, Any]:
    return {
        "total_entries": 0,
        "total_requests": 0,
        "successful_chat_completions": 0,
        "failed_requests": 0,
        "total_errors": 0,
        "total_warnings": 0,
        "payment_processed": 0,
        "upstream_errors": 0,
        "unique_models_count": 0,
        "unique_models": [],
        "error_types": {},
        "success_rate": 0,
        "revenue_msats": 0,
        "refunds_msats": 0,
        "revenue_sats": 0,
        "refunds_sats": 0,
        "net_revenue_msats": 0,
        "net_revenue_sats": 0,
        "avg_revenue_per_request_msats": 0,
        "refund_rate": 0,
    }
