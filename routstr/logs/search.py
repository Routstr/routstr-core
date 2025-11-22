"""
Log search functionality with filtering capabilities.

This module provides generic log search with various filters for viewing
and debugging application logs.
"""

from pathlib import Path

from .reader import get_log_files_in_range, parse_log_file


def search_logs(
    logs_dir: Path,
    date: str | None = None,
    level: str | None = None,
    request_id: str | None = None,
    search_text: str | None = None,
    limit: int = 100,
    max_files: int = 7,
) -> list[dict]:
    """
    Search through log files and return matching entries.
    
    Args:
        logs_dir: Path to the logs directory
        date: Filter by specific date (YYYY-MM-DD format)
        level: Filter by log level (INFO, WARNING, ERROR, etc.)
        request_id: Filter by exact request ID match
        search_text: Search in message and name fields (case-insensitive)
        limit: Maximum number of entries to return
        max_files: Maximum number of log files to search (if no date specified)
        
    Returns:
        List of log entries matching the criteria, sorted by timestamp (newest first)
    """
    log_entries: list[dict] = []

    log_files = get_log_files_in_range(logs_dir, date=date)

    if not date and len(log_files) > max_files:
        log_files = log_files[:max_files]

    search_text_lower = search_text.lower() if search_text else None

    for log_file in log_files:
        entries = parse_log_file(log_file)

        for log_data in entries:
            if not _matches_filters(log_data, level, request_id, search_text_lower):
                continue

            log_entries.append(log_data)

            if len(log_entries) >= limit:
                break

        if len(log_entries) >= limit:
            break

    log_entries.sort(key=lambda x: x.get("asctime", ""), reverse=True)

    return log_entries


def _matches_filters(
    log_data: dict,
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
    if level and log_data.get("levelname", "").upper() != level.upper():
        return False

    if request_id and log_data.get("request_id") != request_id:
        return False

    if search_text_lower:
        message = str(log_data.get("message", "")).lower()
        name = str(log_data.get("name", "")).lower()

        if search_text_lower not in message and search_text_lower not in name:
            return False

    return True
