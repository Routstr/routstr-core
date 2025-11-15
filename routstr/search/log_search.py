"""
Log search functionality.

This module contains the search logic for filtering log entries.
It can be replaced with more advanced search mechanisms in the future
(e.g., Elasticsearch, full-text search databases, etc.)
"""

import json
from pathlib import Path
from typing import Any


def search_logs(
    logs_dir: Path,
    date: str | None = None,
    level: str | None = None,
    request_id: str | None = None,
    search_text: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Search through log files and return matching entries.

    This is a simple file-based search implementation. For better performance
    with large log volumes, consider using:
    - Elasticsearch
    - Splunk
    - Loki
    - Or other log aggregation/search tools

    Args:
        logs_dir: Path to the logs directory
        date: Filter by specific date (YYYY-MM-DD format)
        level: Filter by log level (INFO, WARNING, ERROR, etc.)
        request_id: Filter by exact request ID match
        search_text: Search in message and name fields (case-insensitive)
        limit: Maximum number of entries to return

    Returns:
        List of log entries matching the criteria
    """
    log_entries = []

    if not logs_dir.exists():
        return log_entries

    # Determine which log files to search
    log_files = []
    if date:
        log_file = logs_dir / f"app_{date}.log"
        if log_file.exists():
            log_files.append(log_file)
    else:
        # Search last 7 days of logs
        log_files = sorted(
            logs_dir.glob("app_*.log"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )[:7]

    # Normalize search text for case-insensitive search
    search_text_lower = search_text.lower() if search_text else None

    # Search through log files
    for log_file in log_files:
        try:
            with open(log_file, "r") as f:
                for line in f:
                    try:
                        log_data = json.loads(line.strip())

                        # Apply filters
                        if not _matches_filters(
                            log_data, level, request_id, search_text_lower
                        ):
                            continue

                        log_entries.append(log_data)

                        # Stop if we've reached the limit
                        if len(log_entries) >= limit:
                            break

                    except json.JSONDecodeError:
                        # Skip malformed JSON lines
                        continue

            # Stop searching more files if we've reached the limit
            if len(log_entries) >= limit:
                break

        except Exception:
            # Skip files that can't be read
            continue

    # Sort by timestamp (most recent first)
    log_entries.sort(key=lambda x: x.get("asctime", ""), reverse=True)

    return log_entries


def _matches_filters(
    log_data: dict[str, Any],
    level: str | None,
    request_id: str | None,
    search_text_lower: str | None,
) -> bool:
    """
    Check if a log entry matches the given filters.

    Args:
        log_data: The log entry to check
        level: Log level filter (if any)
        request_id: Request ID filter (if any)
        search_text_lower: Lowercase search text (if any)

    Returns:
        True if the log entry matches all filters, False otherwise
    """
    # Filter by log level
    if level and log_data.get("levelname", "").upper() != level.upper():
        return False

    # Filter by request ID (exact match)
    if request_id and log_data.get("request_id") != request_id:
        return False

    # Filter by search text (case-insensitive search in message and name)
    if search_text_lower:
        message = str(log_data.get("message", "")).lower()
        name = str(log_data.get("name", "")).lower()

        if search_text_lower not in message and search_text_lower not in name:
            return False

    return True
