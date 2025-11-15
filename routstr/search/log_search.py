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
    log_entries: list[dict[str, Any]] = []

    if not logs_dir.exists():
        return log_entries

    log_files = []
    if date:
        log_file = logs_dir / f"app_{date}.log"
        if log_file.exists():
            log_files.append(log_file)
    else:
        log_files = sorted(
            logs_dir.glob("app_*.log"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )[:7]

    search_text_lower = search_text.lower() if search_text else None

    for log_file in log_files:
        try:
            with open(log_file, "r") as f:
                for line in f:
                    try:
                        log_data = json.loads(line.strip())

                        if not _matches_filters(
                            log_data, level, request_id, search_text_lower
                        ):
                            continue

                        log_entries.append(log_data)

                        if len(log_entries) >= limit:
                            break

                    except json.JSONDecodeError:
                        continue

            if len(log_entries) >= limit:
                break

        except Exception:
            continue

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
