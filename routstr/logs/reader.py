"""
Shared log file reading and parsing infrastructure.

This module provides common functionality for reading and parsing JSON log files
from the logs/ directory. It's used by both the log search feature and the usage
analytics dashboard.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)


class LogEntry(TypedDict, total=False):
    """Type definition for a log entry."""

    asctime: str
    name: str
    levelname: str
    message: str
    pathname: str
    lineno: int
    version: str
    request_id: str
    model: str
    cost_data: dict
    max_cost_for_model: int | float
    error_type: str


def parse_log_file(file_path: Path) -> list[dict]:
    """
    Parse a JSON log file and return list of log entries.
    
    Args:
        file_path: Path to the log file
        
    Returns:
        List of parsed log entry dictionaries
    """
    entries: list[dict] = []
    try:
        with open(file_path) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error(f"Error reading log file {file_path}: {e}")
    return entries


def get_log_files_in_range(
    logs_dir: Path, hours_back: int | None = None, date: str | None = None
) -> list[Path]:
    """
    Get log files within a specified time range.
    
    Args:
        logs_dir: Path to the logs directory
        hours_back: Number of hours to look back (if specified)
        date: Specific date to get log file for in YYYY-MM-DD format (if specified)
        
    Returns:
        List of log file paths sorted by modification time (newest first)
    """
    if not logs_dir.exists():
        return []

    if date:
        log_file = logs_dir / f"app_{date}.log"
        return [log_file] if log_file.exists() else []

    all_log_files = sorted(
        logs_dir.glob("app_*.log"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )

    if hours_back is None:
        return all_log_files

    cutoff_date = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    relevant_files: list[Path] = []
    for log_file in all_log_files:
        try:
            file_date_str = log_file.stem.split("_")[1]
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )

            if file_date < cutoff_date.replace(
                hour=0, minute=0, second=0, microsecond=0
            ):
                continue

            relevant_files.append(log_file)
        except Exception as e:
            logger.error(f"Error processing log file {log_file}: {e}")
            continue

    return relevant_files


def filter_entries_by_time(
    entries: list[dict], hours_back: int
) -> list[dict]:
    """
    Filter log entries to only include those within the specified time range.
    
    Args:
        entries: List of log entry dictionaries
        hours_back: Number of hours to look back
        
    Returns:
        Filtered list of log entries
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    filtered: list[dict] = []

    for entry in entries:
        try:
            timestamp_str = entry.get("asctime", "")
            if not timestamp_str:
                continue

            log_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            log_time = log_time.replace(tzinfo=timezone.utc)

            if log_time >= cutoff:
                filtered.append(entry)
        except Exception:
            continue

    return filtered


def get_available_log_dates(logs_dir: Path, max_dates: int = 30) -> list[str]:
    """
    Get list of available log dates.
    
    Args:
        logs_dir: Path to the logs directory
        max_dates: Maximum number of dates to return
        
    Returns:
        List of date strings in YYYY-MM-DD format
    """
    dates: list[str] = []

    if not logs_dir.exists():
        return dates

    log_files = sorted(
        logs_dir.glob("app_*.log"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )

    for log_file in log_files[:max_dates]:
        try:
            filename = log_file.name
            date_str = filename.replace("app_", "").replace(".log", "")
            dates.append(date_str)
        except Exception:
            continue

    return dates
