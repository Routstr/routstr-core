import json
import time
from heapq import heapreplace, heappush
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterator, TypeVar

from .logging import get_logger
from .usage_analytics_store import UsageAnalyticsStore

logger = get_logger(__name__)
T = TypeVar("T")


class LogManager:
    def __init__(self, logs_dir: Path = Path("logs")):
        self.logs_dir = logs_dir
        self._usage_store = UsageAnalyticsStore(logs_dir=logs_dir)
        self._analytics_cache_ttl_seconds = 30.0
        self._analytics_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._analytics_cache_lock = Lock()
        self._cache_miss = object()

    def _get_cached(self, key: tuple[Any, ...]) -> Any:
        now = time.time()
        with self._analytics_cache_lock:
            cached = self._analytics_cache.get(key)
            if cached is None:
                return self._cache_miss

            expires_at, value = cached
            if expires_at <= now:
                self._analytics_cache.pop(key, None)
                return self._cache_miss

            return value

    def _set_cached(
        self, key: tuple[Any, ...], value: Any, ttl_seconds: float | None = None
    ) -> None:
        ttl = (
            self._analytics_cache_ttl_seconds
            if ttl_seconds is None
            else max(1.0, ttl_seconds)
        )
        expires_at = time.time() + ttl
        with self._analytics_cache_lock:
            self._analytics_cache[key] = (expires_at, value)

    def _cache_call(
        self,
        key: tuple[Any, ...],
        compute: Callable[[], T],
        ttl_seconds: float | None = None,
    ) -> T:
        cached = self._get_cached(key)
        if cached is not self._cache_miss:
            return cached

        value = compute()
        self._set_cached(key, value, ttl_seconds=ttl_seconds)
        return value

    def _get_cached_entries(self, hours: int) -> list[dict[str, Any]]:
        return self._cache_call(
            ("usage_entries", hours),
            lambda: list(self._yield_log_entries(hours_back=hours)),
        )

    def _yield_log_entries(
        self,
        hours_back: int | None = None,
        specific_date: str | None = None,
        reverse_files: bool = False,
        max_files: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """
        Yields log entries from files.

        Args:
            hours_back: specific number of hours to look back.
            specific_date: specific date string (YYYY-MM-DD) to look at.
            reverse_files: if True, process files in reverse order (newest first).
            max_files: maximum number of log files to process (most recent if reverse_files is True).
        """
        if not self.logs_dir.exists():
            return

        log_files = []
        cutoff_date = None
        cutoff_timestamp_str: str | None = None

        if specific_date:
            log_file = self.logs_dir / f"app_{specific_date}.log"
            if log_file.exists():
                log_files.append(log_file)
        else:
            log_files = sorted(self.logs_dir.glob("app_*.log"))
            if reverse_files:
                log_files.reverse()

            # If we only care about hours back, we can optimize file selection
            if hours_back is not None:
                cutoff_date = datetime.now(timezone.utc) - timedelta(hours=hours_back)
                cutoff_timestamp_str = cutoff_date.strftime("%Y-%m-%d %H:%M:%S")
                filtered_files = []
                for log_path in log_files:
                    try:
                        file_date_str = log_path.stem.split("_")[1]
                        file_date = datetime.strptime(
                            file_date_str, "%Y-%m-%d"
                        ).replace(tzinfo=timezone.utc)
                        # Include file if it's from the same day or after the cutoff day
                        if file_date >= cutoff_date.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        ):
                            filtered_files.append(log_path)
                    except Exception:
                        continue
                log_files = filtered_files

            if max_files is not None and len(log_files) > max_files:
                log_files = log_files[:max_files]

        for log_file in log_files:
            try:
                with open(log_file, "r") as f:
                    lines_iter = reversed(f.readlines()) if reverse_files else f

                    for line in lines_iter:
                        try:
                            entry = json.loads(line.strip())

                            if cutoff_timestamp_str:
                                timestamp_str = entry.get("asctime", "")
                                if (
                                    not isinstance(timestamp_str, str)
                                    or len(timestamp_str) != 19
                                ):
                                    continue
                                if timestamp_str < cutoff_timestamp_str:
                                    continue

                            yield entry
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.error(f"Error processing log file {log_file}: {e}")
                continue

    def search_logs(
        self,
        date: str | None = None,
        level: str | None = None,
        request_id: str | None = None,
        search_text: str | None = None,
        status_codes: list[int] | None = None,
        methods: list[str] | None = None,
        endpoints: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Search through log files and return matching entries.
        """
        log_entries: list[dict[str, Any]] = []

        # Use reverse=True to get newest logs first by default
        # If date is specified, we only look at that file

        search_text_lower = search_text.lower() if search_text else None

        # We iterate efficiently
        iterator = self._yield_log_entries(
            specific_date=date,
            reverse_files=True if not date else False,
            max_files=7 if not date else None,
        )

        # If we are searching globally (no date), we might want to limit how far back we go?
        # PR 228 did: "glob("app_*.log") sorted by mtime reverse [:7]" (last 7 files)
        # My _yield_log_entries with reverse_files=True does all files.
        # Let's rely on limit to stop us.

        # Optimization: if we are not searching by date, maybe limit to last 7 files inside _yield?
        # For now, let's just iterate.

        for log_data in iterator:
            if not self._matches_filters(
                log_data,
                level,
                request_id,
                search_text_lower,
                status_codes,
                methods,
                endpoints,
            ):
                continue

            log_entries.append(log_data)

            if len(log_entries) >= limit:
                break

        # Sort by time descending (newest first)
        log_entries.sort(key=lambda x: x.get("asctime", ""), reverse=True)
        return log_entries

    def _matches_filters(
        self,
        log_data: dict[str, Any],
        level: str | None,
        request_id: str | None,
        search_text_lower: str | None,
        status_codes: list[int] | None = None,
        methods: list[str] | None = None,
        endpoints: list[str] | None = None,
    ) -> bool:
        if level and str(log_data.get("levelname", "")).upper() != level.upper():
            return False

        if request_id and log_data.get("request_id") != request_id:
            return False

        if status_codes:
            entry_status = log_data.get("status_code")
            if entry_status is not None:
                try:
                    if int(entry_status) not in status_codes:
                        return False
                except (ValueError, TypeError):
                    return False
            else:
                return False

        if methods:
            entry_method = log_data.get("method", "").upper()
            if entry_method not in [m.upper() for m in methods]:
                return False

        if endpoints:
            entry_path = log_data.get("path", "")
            matched = False
            for endpoint in endpoints:
                clean_endpoint = endpoint.lstrip("/")
                if entry_path.startswith(clean_endpoint):
                    matched = True
                    break
                if clean_endpoint in entry_path:
                    matched = True
                    break
            if not matched:
                return False

        if search_text_lower:
            message = str(log_data.get("message", "")).lower()
            name = str(log_data.get("name", "")).lower()
            pathname = str(log_data.get("pathname", "")).lower()

            if (
                search_text_lower not in message
                and search_text_lower not in name
                and search_text_lower not in pathname
            ):
                return False

        return True

    def _bucket_key_for_timestamp(
        self, timestamp_str: str, interval_minutes: int
    ) -> str | None:
        if len(timestamp_str) != 19:
            return None
        if timestamp_str[10] != " ":
            return None

        try:
            hour = int(timestamp_str[11:13])
            minute = int(timestamp_str[14:16])
        except (TypeError, ValueError):
            return None

        total_minutes = hour * 60 + minute
        rounded_minutes = (total_minutes // interval_minutes) * interval_minutes
        rounded_hour = rounded_minutes // 60
        rounded_minute = rounded_minutes % 60
        return f"{timestamp_str[:10]} {rounded_hour:02d}:{rounded_minute:02d}:00"

    def get_usage_summary(self, hours: int = 24) -> dict:
        def compute() -> dict:
            try:
                return self._usage_store.get_summary(hours_back=hours)
            except Exception as e:
                logger.error(
                    f"Usage analytics index failed, falling back to log scan: {e}"
                )
                return self._calculate_summary_stats(self._get_cached_entries(hours))

        return self._cache_call(
            ("usage_summary", hours),
            compute,
        )

    def get_usage_metrics(self, interval: int = 15, hours: int = 24) -> dict:
        def compute() -> dict:
            try:
                return self._usage_store.get_metrics(
                    interval_minutes=interval,
                    hours_back=hours,
                )
            except Exception as e:
                logger.error(
                    f"Usage analytics index failed, falling back to log scan: {e}"
                )
                return self._aggregate_metrics_by_time(
                    self._get_cached_entries(hours), interval, hours
                )

        return self._cache_call(
            ("usage_metrics", interval, hours),
            compute,
        )

    def get_usage_dashboard(
        self,
        interval: int = 15,
        hours: int = 24,
        error_limit: int = 100,
        model_limit: int = 20,
    ) -> dict:
        # Large ranges are expensive to scan; keep cached longer.
        if hours <= 24:
            cache_ttl = 60.0
        elif hours <= 7 * 24:
            cache_ttl = 300.0
        elif hours <= 30 * 24:
            cache_ttl = 1800.0
        elif hours <= 90 * 24:
            cache_ttl = 7200.0
        else:
            cache_ttl = 21600.0

        def compute() -> dict:
            try:
                return self._usage_store.get_dashboard(
                    interval_minutes=interval,
                    hours_back=hours,
                    error_limit=error_limit,
                    model_limit=model_limit,
                )
            except Exception as e:
                logger.error(
                    f"Usage analytics index failed, falling back to log scan: {e}"
                )
                return self._aggregate_dashboard(
                    interval_minutes=interval,
                    hours_back=hours,
                    error_limit=error_limit,
                    model_limit=model_limit,
                )

        return self._cache_call(
            ("usage_dashboard", interval, hours, error_limit, model_limit),
            compute,
            ttl_seconds=cache_ttl,
        )

    def get_error_details(self, hours: int = 24, limit: int = 100) -> dict:
        def compute() -> dict:
            try:
                return self._usage_store.get_error_details(hours_back=hours, limit=limit)
            except Exception as e:
                logger.error(
                    f"Usage analytics index failed, falling back to log scan: {e}"
                )

            errors: list[dict] = []
            for entry in self._get_cached_entries(hours):
                if str(entry.get("levelname", "")).upper() == "ERROR":
                    timestamp_str = entry.get("asctime", "")
                    errors.append(
                        {
                            "timestamp": timestamp_str,
                            "message": entry.get("message", ""),
                            "error_type": entry.get("error_type", "unknown"),
                            "pathname": entry.get("pathname", ""),
                            "lineno": entry.get("lineno", 0),
                            "request_id": entry.get("request_id", ""),
                        }
                    )

            errors.sort(key=lambda x: x["timestamp"], reverse=True)
            return {"errors": errors[:limit], "total_count": len(errors)}

        return self._cache_call(("error_details", hours, limit), compute)

    def get_revenue_by_model(self, hours: int = 24, limit: int = 20) -> dict:
        def compute() -> dict:
            try:
                return self._usage_store.get_revenue_by_model(
                    hours_back=hours, limit=limit
                )
            except Exception as e:
                logger.error(
                    f"Usage analytics index failed, falling back to log scan: {e}"
                )

            entries = self._get_cached_entries(hours)

            model_stats: dict[str, dict[str, int | float]] = defaultdict(
                lambda: {
                    "revenue_msats": 0,
                    "refunds_msats": 0,
                    "requests": 0,
                    "successful": 0,
                    "failed": 0,
                }
            )

            for entry in entries:
                try:
                    model = entry.get("model", "unknown")
                    if not isinstance(model, str):
                        model = "unknown"

                    message = str(entry.get("message", "")).lower()

                    completed = (
                        "completed for streaming" in message
                        or "completed for non-streaming" in message
                    )
                    if completed:
                        model_stats[model]["requests"] += 1
                        model_stats[model]["successful"] += 1
                        cost_data = entry.get("cost_data")
                        if isinstance(cost_data, dict):
                            actual_cost = cost_data.get("total_msats", 0)
                            if (
                                isinstance(actual_cost, (int, float))
                                and actual_cost > 0
                            ):
                                model_stats[model]["revenue_msats"] += actual_cost

                    failed = (
                        "revert payment" in message
                        or "upstream request failed" in message
                    )
                    if failed:
                        model_stats[model]["requests"] += 1
                        model_stats[model]["failed"] += 1
                        if "revert payment" in message:
                            max_cost = entry.get("max_cost_for_model", 0)
                            if isinstance(max_cost, (int, float)) and max_cost > 0:
                                model_stats[model]["refunds_msats"] += max_cost

                except Exception:
                    continue

            models: list[dict[str, Any]] = []
            total_revenue = 0.0

            for model, stats in model_stats.items():
                revenue_msats = float(stats["revenue_msats"])
                refunds_msats = float(stats["refunds_msats"])

                revenue_sats = revenue_msats / 1000
                refunds_sats = refunds_msats / 1000
                net_revenue_sats = revenue_sats - refunds_sats

                total_revenue += net_revenue_sats

                requests = int(stats["requests"])
                successful = int(stats["successful"])

                models.append(
                    {
                        "model": model,
                        "revenue_sats": revenue_sats,
                        "refunds_sats": refunds_sats,
                        "net_revenue_sats": net_revenue_sats,
                        "requests": requests,
                        "successful": successful,
                        "failed": int(stats["failed"]),
                        "avg_revenue_per_request": (
                            revenue_sats / successful if successful > 0 else 0
                        ),
                    }
                )

            models.sort(key=lambda x: float(x["net_revenue_sats"]), reverse=True)

            return {
                "models": models[:limit],
                "total_revenue_sats": total_revenue,
                "total_models": len(models),
            }

        return self._cache_call(("revenue_by_model", hours, limit), compute)

    def _build_summary_response(self, stats: dict[str, Any]) -> dict[str, Any]:
        revenue_sats = stats["revenue_msats"] / 1000
        refunds_sats = stats["refunds_msats"] / 1000
        net_revenue_sats = revenue_sats - refunds_sats

        total_requests = stats["total_requests"]
        successful = stats["successful_chat_completions"]

        return {
            "total_entries": stats["total_entries"],
            "total_requests": total_requests,
            "successful_chat_completions": successful,
            "failed_requests": stats["failed_requests"],
            "total_errors": stats["total_errors"],
            "total_warnings": stats["total_warnings"],
            "payment_processed": stats["payment_processed"],
            "upstream_errors": stats["upstream_errors"],
            "unique_models_count": len(stats["unique_models"]),
            "unique_models": sorted(list(stats["unique_models"])),
            "error_types": dict(stats["error_types"]),
            "success_rate": (successful / total_requests * 100)
            if total_requests > 0
            else 0,
            "revenue_msats": stats["revenue_msats"],
            "refunds_msats": stats["refunds_msats"],
            "revenue_sats": revenue_sats,
            "refunds_sats": refunds_sats,
            "net_revenue_msats": stats["revenue_msats"] - stats["refunds_msats"],
            "net_revenue_sats": net_revenue_sats,
            "avg_revenue_per_request_msats": (
                stats["revenue_msats"] / successful if successful > 0 else 0
            ),
            "refund_rate": (
                (stats["failed_requests"] / total_requests * 100)
                if total_requests > 0
                else 0
            ),
        }

    def _calculate_summary_stats(self, entries: list[dict]) -> dict:
        stats: dict[str, Any] = {
            "total_entries": 0,
            "total_requests": 0,
            "successful_chat_completions": 0,
            "failed_requests": 0,
            "total_errors": 0,
            "total_warnings": 0,
            "payment_processed": 0,
            "upstream_errors": 0,
            "unique_models": set(),
            "error_types": defaultdict(int),
            "revenue_msats": 0.0,
            "refunds_msats": 0.0,
        }

        for entry in entries:
            try:
                stats["total_entries"] += 1

                message = str(entry.get("message", "")).lower()
                level = str(entry.get("levelname", "")).upper()

                if level == "ERROR":
                    stats["total_errors"] += 1
                    if "error_type" in entry:
                        stats["error_types"][str(entry["error_type"])] += 1
                elif level == "WARNING":
                    stats["total_warnings"] += 1

                completed = (
                    "completed for streaming" in message
                    or "completed for non-streaming" in message
                )
                if completed:
                    stats["total_requests"] += 1
                    stats["successful_chat_completions"] += 1

                failed = (
                    "upstream request failed" in message
                    or "revert payment" in message
                )
                if failed:
                    stats["total_requests"] += 1
                    stats["failed_requests"] += 1

                if "payment processed successfully" in message:
                    stats["payment_processed"] += 1

                if "upstream" in message and level == "ERROR":
                    stats["upstream_errors"] += 1

                if "model" in entry:
                    model = entry["model"]
                    if isinstance(model, str) and model != "unknown":
                        stats["unique_models"].add(model)

                if completed:
                    cost_data = entry.get("cost_data")
                    if isinstance(cost_data, dict):
                        actual_cost = cost_data.get("total_msats", 0)
                        if isinstance(actual_cost, (int, float)) and actual_cost > 0:
                            stats["revenue_msats"] += float(actual_cost)

                if "revert payment" in message:
                    max_cost = entry.get("max_cost_for_model", 0)
                    if isinstance(max_cost, (int, float)) and max_cost > 0:
                        stats["refunds_msats"] += float(max_cost)

            except Exception:
                continue

        return self._build_summary_response(stats)

    def _aggregate_dashboard(
        self,
        interval_minutes: int,
        hours_back: int,
        error_limit: int,
        model_limit: int,
    ) -> dict[str, Any]:
        time_buckets: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "total_requests": 0,
                "successful_chat_completions": 0,
                "failed_requests": 0,
                "errors": 0,
                "warnings": 0,
                "payment_processed": 0,
                "upstream_errors": 0,
                "revenue_msats": 0.0,
                "refunds_msats": 0.0,
            }
        )
        summary_stats: dict[str, Any] = {
            "total_entries": 0,
            "total_requests": 0,
            "successful_chat_completions": 0,
            "failed_requests": 0,
            "total_errors": 0,
            "total_warnings": 0,
            "payment_processed": 0,
            "upstream_errors": 0,
            "unique_models": set(),
            "error_types": defaultdict(int),
            "revenue_msats": 0.0,
            "refunds_msats": 0.0,
        }
        model_stats: dict[str, dict[str, int | float]] = defaultdict(
            lambda: {
                "revenue_msats": 0,
                "refunds_msats": 0,
                "requests": 0,
                "successful": 0,
                "failed": 0,
            }
        )
        latest_errors_heap: list[tuple[str, dict[str, Any]]] = []
        total_error_count = 0

        for entry in self._yield_log_entries(hours_back=hours_back):
            try:
                summary_stats["total_entries"] += 1

                timestamp_str = entry.get("asctime", "")
                message = str(entry.get("message", "")).lower()
                level = str(entry.get("levelname", "")).upper()
                model = entry.get("model", "unknown")
                if not isinstance(model, str):
                    model = "unknown"

                bucket_key = (
                    self._bucket_key_for_timestamp(timestamp_str, interval_minutes)
                    if isinstance(timestamp_str, str)
                    else None
                )
                bucket = time_buckets[bucket_key] if bucket_key else None

                if level == "ERROR":
                    summary_stats["total_errors"] += 1
                    if bucket:
                        bucket["errors"] += 1
                    if "error_type" in entry:
                        summary_stats["error_types"][str(entry["error_type"])] += 1

                    total_error_count += 1
                    error_item = {
                        "timestamp": timestamp_str,
                        "message": entry.get("message", ""),
                        "error_type": entry.get("error_type", "unknown"),
                        "pathname": entry.get("pathname", ""),
                        "lineno": entry.get("lineno", 0),
                        "request_id": entry.get("request_id", ""),
                    }
                    if len(latest_errors_heap) < error_limit:
                        heappush(latest_errors_heap, (timestamp_str, error_item))
                    elif timestamp_str > latest_errors_heap[0][0]:
                        heapreplace(latest_errors_heap, (timestamp_str, error_item))
                elif level == "WARNING":
                    summary_stats["total_warnings"] += 1
                    if bucket:
                        bucket["warnings"] += 1

                completed = (
                    "completed for streaming" in message
                    or "completed for non-streaming" in message
                )
                if completed:
                    summary_stats["total_requests"] += 1
                    summary_stats["successful_chat_completions"] += 1
                    model_stats[model]["requests"] += 1
                    model_stats[model]["successful"] += 1
                    if bucket:
                        bucket["total_requests"] += 1
                        bucket["successful_chat_completions"] += 1

                    cost_data = entry.get("cost_data")
                    if isinstance(cost_data, dict):
                        actual_cost = cost_data.get("total_msats", 0)
                        if isinstance(actual_cost, (int, float)) and actual_cost > 0:
                            actual_cost_float = float(actual_cost)
                            summary_stats["revenue_msats"] += actual_cost_float
                            model_stats[model]["revenue_msats"] += actual_cost_float
                            if bucket:
                                bucket["revenue_msats"] += actual_cost_float

                failed = (
                    "upstream request failed" in message
                    or "revert payment" in message
                )
                if failed:
                    summary_stats["total_requests"] += 1
                    summary_stats["failed_requests"] += 1
                    model_stats[model]["requests"] += 1
                    model_stats[model]["failed"] += 1
                    if bucket:
                        bucket["total_requests"] += 1
                        bucket["failed_requests"] += 1

                if "payment processed successfully" in message:
                    summary_stats["payment_processed"] += 1
                    if bucket:
                        bucket["payment_processed"] += 1

                if "upstream" in message and level == "ERROR":
                    summary_stats["upstream_errors"] += 1
                    if bucket:
                        bucket["upstream_errors"] += 1

                if model != "unknown":
                    summary_stats["unique_models"].add(model)

                if "revert payment" in message:
                    max_cost = entry.get("max_cost_for_model", 0)
                    if isinstance(max_cost, (int, float)) and max_cost > 0:
                        max_cost_float = float(max_cost)
                        summary_stats["refunds_msats"] += max_cost_float
                        model_stats[model]["refunds_msats"] += max_cost_float
                        if bucket:
                            bucket["refunds_msats"] += max_cost_float
            except Exception:
                continue

        metrics_result = []
        for bucket_key in sorted(time_buckets.keys()):
            bucket = dict(time_buckets[bucket_key])
            bucket["requests"] = bucket["total_requests"]
            metrics_result.append({"timestamp": bucket_key, **bucket})

        models: list[dict[str, Any]] = []
        total_revenue = 0.0
        for model_name, stats in model_stats.items():
            revenue_msats = float(stats["revenue_msats"])
            refunds_msats = float(stats["refunds_msats"])
            revenue_sats = revenue_msats / 1000
            refunds_sats = refunds_msats / 1000
            net_revenue_sats = revenue_sats - refunds_sats
            total_revenue += net_revenue_sats

            successful = int(stats["successful"])
            models.append(
                {
                    "model": model_name,
                    "revenue_sats": revenue_sats,
                    "refunds_sats": refunds_sats,
                    "net_revenue_sats": net_revenue_sats,
                    "requests": int(stats["requests"]),
                    "successful": successful,
                    "failed": int(stats["failed"]),
                    "avg_revenue_per_request": (
                        revenue_sats / successful if successful > 0 else 0
                    ),
                }
            )

        models.sort(key=lambda x: float(x["net_revenue_sats"]), reverse=True)
        latest_errors = [
            item
            for _, item in sorted(
                latest_errors_heap, key=lambda x: x[0], reverse=True
            )
        ]

        return {
            "metrics": {
                "metrics": metrics_result,
                "interval_minutes": interval_minutes,
                "hours_back": hours_back,
                "total_buckets": len(metrics_result),
            },
            "summary": self._build_summary_response(summary_stats),
            "error_details": {
                "errors": latest_errors,
                "total_count": total_error_count,
            },
            "revenue_by_model": {
                "models": models[:model_limit],
                "total_revenue_sats": total_revenue,
                "total_models": len(models),
            },
        }

    def _aggregate_metrics_by_time(
        self, entries: list[dict], interval_minutes: int, hours_back: int
    ) -> dict:
        time_buckets: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "total_requests": 0,
                "successful_chat_completions": 0,
                "failed_requests": 0,
                "errors": 0,
                "warnings": 0,
                "payment_processed": 0,
                "upstream_errors": 0,
                "revenue_msats": 0.0,
                "refunds_msats": 0.0,
            }
        )

        for entry in entries:
            try:
                timestamp_str = entry.get("asctime", "")
                if not isinstance(timestamp_str, str):
                    continue
                bucket_key = self._bucket_key_for_timestamp(
                    timestamp_str, interval_minutes
                )
                if not bucket_key:
                    continue

                bucket = time_buckets[bucket_key]

                message = str(entry.get("message", "")).lower()
                level = str(entry.get("levelname", "")).upper()

                completed = (
                    "completed for streaming" in message
                    or "completed for non-streaming" in message
                )
                if completed:
                    bucket["total_requests"] += 1
                    bucket["successful_chat_completions"] += 1

                if level == "ERROR":
                    bucket["errors"] += 1
                    if "upstream" in message:
                        bucket["upstream_errors"] += 1
                elif level == "WARNING":
                    bucket["warnings"] += 1

                failed = (
                    "upstream request failed" in message
                    or "revert payment" in message
                )
                if failed:
                    bucket["total_requests"] += 1
                    bucket["failed_requests"] += 1

                if "payment processed successfully" in message:
                    bucket["payment_processed"] += 1

                if completed:
                    cost_data = entry.get("cost_data")
                    if isinstance(cost_data, dict):
                        actual_cost = cost_data.get("total_msats", 0)
                        if isinstance(actual_cost, (int, float)) and actual_cost > 0:
                            bucket["revenue_msats"] += float(actual_cost)

                if "revert payment" in message:
                    max_cost = entry.get("max_cost_for_model", 0)
                    if isinstance(max_cost, (int, float)) and max_cost > 0:
                        bucket["refunds_msats"] += float(max_cost)
            except Exception:
                continue

        result = []
        for bucket_key in sorted(time_buckets.keys()):
            bucket = dict(time_buckets[bucket_key])
            # Backward-compatible alias for any callers still reading "requests".
            bucket["requests"] = bucket["total_requests"]
            result.append({"timestamp": bucket_key, **bucket})

        totals = {
            "total_requests": 0,
            "successful_chat_completions": 0,
            "failed_requests": 0,
            "errors": 0,
            "warnings": 0,
            "payment_processed": 0,
            "upstream_errors": 0,
            "revenue_msats": 0.0,
            "refunds_msats": 0.0,
        }
        for bucket in result:
            totals["total_requests"] += int(bucket["total_requests"])
            totals["successful_chat_completions"] += int(
                bucket["successful_chat_completions"]
            )
            totals["failed_requests"] += int(bucket["failed_requests"])
            totals["errors"] += int(bucket["errors"])
            totals["warnings"] += int(bucket["warnings"])
            totals["payment_processed"] += int(bucket["payment_processed"])
            totals["upstream_errors"] += int(bucket["upstream_errors"])
            totals["revenue_msats"] += float(bucket["revenue_msats"])
            totals["refunds_msats"] += float(bucket["refunds_msats"])

        return {
            "metrics": result,
            "interval_minutes": interval_minutes,
            "hours_back": hours_back,
            "total_buckets": len(result),
            "totals": totals,
        }


log_manager = LogManager()
