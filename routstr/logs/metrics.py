"""
Usage metrics extraction and aggregation from log files.

This module provides analytics-specific parsing of log entries to extract
metrics like request volume, revenue, errors, and performance statistics.

CRITICAL: This module parses specific log messages for usage tracking.
The following log messages must not be modified without updating this module:
- "received proxy request" -> counts total_requests
- "token adjustment completed" -> counts successful_chat_completions, extracts revenue from cost_data.total_msats
- "upstream request failed" OR "revert payment" -> counts failed_requests, extracts refunds from max_cost_for_model
- "payment processed successfully" -> counts payment_processed
- ERROR level with "upstream" -> counts upstream_errors

See routstr/core/logging.py for full documentation of critical log messages.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .reader import filter_entries_by_time, get_log_files_in_range, parse_log_file

logger = logging.getLogger(__name__)


def aggregate_metrics_by_time(
    logs_dir: Path, interval_minutes: int, hours_back: int = 24
) -> dict:
    """
    Aggregate log metrics into time buckets.
    
    Args:
        logs_dir: Path to the logs directory
        interval_minutes: Size of time buckets in minutes (e.g., 15 for 15-minute intervals)
        hours_back: Number of hours of history to analyze
        
    Returns:
        Dictionary containing metrics array and metadata
    """
    log_files = get_log_files_in_range(logs_dir, hours_back=hours_back)
    all_entries: list[dict] = []

    for log_file in log_files:
        entries = parse_log_file(log_file)
        all_entries.extend(entries)

    filtered_entries = filter_entries_by_time(all_entries, hours_back)

    time_buckets: dict[str, dict[str, int | float]] = defaultdict(
        lambda: {
            "total_requests": 0,
            "successful_chat_completions": 0,
            "failed_requests": 0,
            "errors": 0,
            "warnings": 0,
            "payment_processed": 0,
            "upstream_errors": 0,
            "revenue_msats": 0,
            "refunds_msats": 0,
        }
    )

    for entry in filtered_entries:
        try:
            timestamp_str = entry.get("asctime", "")
            if not timestamp_str:
                continue

            log_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            log_time = log_time.replace(tzinfo=timezone.utc)

            bucket_time = log_time.replace(
                minute=(log_time.minute // interval_minutes) * interval_minutes,
                second=0,
                microsecond=0,
            )
            bucket_key = bucket_time.isoformat()

            message = entry.get("message", "").lower()
            level = entry.get("levelname", "").upper()

            if level == "ERROR":
                time_buckets[bucket_key]["errors"] += 1
            elif level == "WARNING":
                time_buckets[bucket_key]["warnings"] += 1

            if "received proxy request" in message:
                time_buckets[bucket_key]["total_requests"] += 1

            if "token adjustment completed for non-streaming" in message:
                time_buckets[bucket_key]["successful_chat_completions"] += 1
            elif "token adjustment completed for streaming" in message:
                time_buckets[bucket_key]["successful_chat_completions"] += 1

            if "upstream request failed" in message or "revert payment" in message:
                time_buckets[bucket_key]["failed_requests"] += 1

            if "payment processed successfully" in message:
                time_buckets[bucket_key]["payment_processed"] += 1

            if "upstream" in message and level == "ERROR":
                time_buckets[bucket_key]["upstream_errors"] += 1

            if "token adjustment completed" in message:
                cost_data = entry.get("cost_data")
                if isinstance(cost_data, dict):
                    actual_cost = cost_data.get("total_msats", 0)
                    if isinstance(actual_cost, (int, float)) and actual_cost > 0:
                        time_buckets[bucket_key]["revenue_msats"] += actual_cost

            if "revert payment" in message:
                max_cost = entry.get("max_cost_for_model", 0)
                if isinstance(max_cost, (int, float)) and max_cost > 0:
                    time_buckets[bucket_key]["refunds_msats"] += max_cost

        except Exception:
            continue

    result: list[dict] = []
    for bucket_key in sorted(time_buckets.keys()):
        result.append({"timestamp": bucket_key, **time_buckets[bucket_key]})

    return {
        "metrics": result,
        "interval_minutes": interval_minutes,
        "hours_back": hours_back,
        "total_buckets": len(result),
    }


def get_summary_stats(logs_dir: Path, hours_back: int = 24) -> dict:
    """
    Calculate summary statistics from log entries.
    
    Args:
        logs_dir: Path to the logs directory
        hours_back: Number of hours of history to analyze
        
    Returns:
        Dictionary containing summary statistics
    """
    log_files = get_log_files_in_range(logs_dir, hours_back=hours_back)
    all_entries: list[dict] = []

    for log_file in log_files:
        entries = parse_log_file(log_file)
        all_entries.extend(entries)

    filtered_entries = filter_entries_by_time(all_entries, hours_back)

    stats: dict[str, int | float | set[str] | defaultdict[str, int]] = {
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
        "revenue_msats": 0,
        "refunds_msats": 0,
    }

    for entry in filtered_entries:
        try:
            total_entries = stats["total_entries"]
            assert isinstance(total_entries, int)
            stats["total_entries"] = total_entries + 1

            message = entry.get("message", "").lower()
            level = entry.get("levelname", "").upper()

            if level == "ERROR":
                assert isinstance(stats["total_errors"], int)
                stats["total_errors"] += 1
                if "error_type" in entry:
                    error_type = str(entry["error_type"])
                    error_types = stats["error_types"]
                    assert isinstance(error_types, defaultdict)
                    error_types[error_type] += 1
            elif level == "WARNING":
                assert isinstance(stats["total_warnings"], int)
                stats["total_warnings"] += 1

            if "received proxy request" in message:
                assert isinstance(stats["total_requests"], int)
                stats["total_requests"] += 1

            if "token adjustment completed" in message:
                assert isinstance(stats["successful_chat_completions"], int)
                stats["successful_chat_completions"] += 1

            if "upstream request failed" in message or "revert payment" in message:
                assert isinstance(stats["failed_requests"], int)
                stats["failed_requests"] += 1

            if "payment processed successfully" in message:
                assert isinstance(stats["payment_processed"], int)
                stats["payment_processed"] += 1

            if "upstream" in message and level == "ERROR":
                assert isinstance(stats["upstream_errors"], int)
                stats["upstream_errors"] += 1

            if "model" in entry:
                model = entry["model"]
                if isinstance(model, str) and model != "unknown":
                    unique_models = stats["unique_models"]
                    assert isinstance(unique_models, set)
                    unique_models.add(model)

            if "token adjustment completed" in message:
                cost_data = entry.get("cost_data")
                if isinstance(cost_data, dict):
                    actual_cost = cost_data.get("total_msats", 0)
                    if isinstance(actual_cost, (int, float)) and actual_cost > 0:
                        assert isinstance(stats["revenue_msats"], (int, float))
                        stats["revenue_msats"] = (
                            float(stats["revenue_msats"]) + float(actual_cost)
                        )

            if "revert payment" in message:
                max_cost = entry.get("max_cost_for_model", 0)
                if isinstance(max_cost, (int, float)) and max_cost > 0:
                    assert isinstance(stats["refunds_msats"], (int, float))
                    stats["refunds_msats"] = (
                        float(stats["refunds_msats"]) + float(max_cost)
                    )

        except Exception:
            continue

    revenue_msats = float(stats["revenue_msats"])
    refunds_msats = float(stats["refunds_msats"])
    revenue_sats = revenue_msats / 1000
    refunds_sats = refunds_msats / 1000
    net_revenue_msats = revenue_msats - refunds_msats
    net_revenue_sats = revenue_sats - refunds_sats

    total_requests = int(stats["total_requests"])
    successful = int(stats["successful_chat_completions"])
    failed = int(stats["failed_requests"])
    success_rate = (successful / total_requests * 100) if total_requests > 0 else 0

    unique_models_set = stats["unique_models"]
    assert isinstance(unique_models_set, set)

    error_types_dict = stats["error_types"]
    assert isinstance(error_types_dict, defaultdict)

    return {
        "total_entries": stats["total_entries"],
        "total_requests": total_requests,
        "successful_chat_completions": successful,
        "failed_requests": failed,
        "total_errors": stats["total_errors"],
        "total_warnings": stats["total_warnings"],
        "payment_processed": stats["payment_processed"],
        "upstream_errors": stats["upstream_errors"],
        "unique_models_count": len(unique_models_set),
        "unique_models": sorted(list(unique_models_set)),
        "error_types": dict(error_types_dict),
        "revenue_msats": revenue_msats,
        "refunds_msats": refunds_msats,
        "revenue_sats": revenue_sats,
        "refunds_sats": refunds_sats,
        "net_revenue_msats": net_revenue_msats,
        "net_revenue_sats": net_revenue_sats,
        "success_rate": success_rate,
    }


def get_error_details(
    logs_dir: Path, hours_back: int = 24, limit: int = 100
) -> dict:
    """
    Get detailed error information from logs.
    
    Args:
        logs_dir: Path to the logs directory
        hours_back: Number of hours of history to analyze
        limit: Maximum number of errors to return
        
    Returns:
        Dictionary containing error details
    """
    log_files = get_log_files_in_range(logs_dir, hours_back=hours_back)
    errors: list[dict] = []

    for log_file in log_files:
        entries = parse_log_file(log_file)

        for entry in entries:
            if entry.get("levelname", "").upper() == "ERROR":
                timestamp_str = entry.get("asctime", "")
                if timestamp_str:
                    try:
                        log_time = datetime.strptime(
                            timestamp_str, "%Y-%m-%d %H:%M:%S"
                        )
                        log_time = log_time.replace(tzinfo=timezone.utc)
                        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

                        if log_time >= cutoff:
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
                    except Exception:
                        continue

            if len(errors) >= limit:
                break

        if len(errors) >= limit:
            break

    errors.sort(key=lambda x: x["timestamp"], reverse=True)

    return {"errors": errors[:limit], "total_count": len(errors)}


def get_revenue_by_model(logs_dir: Path, hours_back: int = 24, limit: int = 20) -> dict:
    """
    Get revenue breakdown by model.
    
    Args:
        logs_dir: Path to the logs directory
        hours_back: Number of hours of history to analyze
        limit: Maximum number of models to return
        
    Returns:
        Dictionary containing revenue breakdown by model
    """
    log_files = get_log_files_in_range(logs_dir, hours_back=hours_back)
    all_entries: list[dict] = []

    for log_file in log_files:
        entries = parse_log_file(log_file)
        all_entries.extend(entries)

    filtered_entries = filter_entries_by_time(all_entries, hours_back)

    model_stats: dict[str, dict[str, int | float]] = defaultdict(
        lambda: {
            "revenue_msats": 0,
            "refunds_msats": 0,
            "requests": 0,
            "successful": 0,
            "failed": 0,
        }
    )

    for entry in filtered_entries:
        try:
            model = entry.get("model", "unknown")
            if not isinstance(model, str):
                model = "unknown"

            message = entry.get("message", "").lower()

            if "received proxy request" in message:
                model_stats[model]["requests"] += 1

            if "token adjustment completed" in message:
                model_stats[model]["successful"] += 1
                cost_data = entry.get("cost_data")
                if isinstance(cost_data, dict):
                    actual_cost = cost_data.get("total_msats", 0)
                    if isinstance(actual_cost, (int, float)) and actual_cost > 0:
                        model_stats[model]["revenue_msats"] += actual_cost

            if "revert payment" in message or "upstream request failed" in message:
                model_stats[model]["failed"] += 1
                if "revert payment" in message:
                    max_cost = entry.get("max_cost_for_model", 0)
                    if isinstance(max_cost, (int, float)) and max_cost > 0:
                        model_stats[model]["refunds_msats"] += max_cost

        except Exception:
            continue

    models: list[dict[str, str | int | float]] = []
    total_revenue = 0.0

    for model, stats in model_stats.items():
        revenue_msats_raw = stats["revenue_msats"]
        assert isinstance(revenue_msats_raw, (int, float))
        revenue_msats_val = float(revenue_msats_raw)

        refunds_msats_raw = stats["refunds_msats"]
        assert isinstance(refunds_msats_raw, (int, float))
        refunds_msats_val = float(refunds_msats_raw)

        revenue_sats = revenue_msats_val / 1000
        refunds_sats = refunds_msats_val / 1000
        net_revenue_sats = revenue_sats - refunds_sats

        total_revenue += net_revenue_sats

        requests_raw = stats["requests"]
        assert isinstance(requests_raw, int)
        requests_val = requests_raw

        successful_raw = stats["successful"]
        assert isinstance(successful_raw, int)
        successful_val = successful_raw

        failed_raw = stats["failed"]
        assert isinstance(failed_raw, int)
        failed_val = failed_raw

        model_data: dict[str, str | int | float] = {
            "model": model,
            "revenue_sats": revenue_sats,
            "refunds_sats": refunds_sats,
            "net_revenue_sats": net_revenue_sats,
            "requests": requests_val,
            "successful": successful_val,
            "failed": failed_val,
            "avg_revenue_per_request": (
                revenue_sats / successful_val if successful_val > 0 else 0
            ),
        }
        models.append(model_data)

    def _sort_key(x: dict[str, str | int | float]) -> float:
        val = x["net_revenue_sats"]
        assert isinstance(val, (int, float))
        return float(val)

    models.sort(key=_sort_key, reverse=True)

    return {
        "models": models[:limit],
        "total_revenue_sats": total_revenue,
        "total_models": len(models),
    }
