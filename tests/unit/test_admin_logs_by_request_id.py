from typing import Any
from unittest.mock import Mock, patch

import pytest

from routstr.core.admin import get_logs_by_request_id_api


@pytest.mark.asyncio
async def test_returns_entries_for_request_id_oldest_first() -> None:
    entries = [
        {"asctime": "2026-01-01 10:00:02", "message": "done", "request_id": "req-1"},
        {"asctime": "2026-01-01 10:00:01", "message": "start", "request_id": "req-1"},
    ]

    with patch("routstr.core.admin.log_manager") as log_manager:
        log_manager.search_logs.return_value = entries
        result: dict[str, Any] = await get_logs_by_request_id_api(
            request=Mock(), request_id="req-1", date=None, limit=200
        )

    log_manager.search_logs.assert_called_once_with(
        date=None, request_id="req-1", limit=200
    )
    assert result["total"] == 2
    assert result["request_id"] == "req-1"
    assert [entry["message"] for entry in result["logs"]] == ["start", "done"]  # type: ignore[index,union-attr]


@pytest.mark.asyncio
async def test_returns_empty_list_for_unknown_request_id() -> None:
    with patch("routstr.core.admin.log_manager") as log_manager:
        log_manager.search_logs.return_value = []
        result = await get_logs_by_request_id_api(
            request=Mock(), request_id="missing", date="2026-01-01", limit=10
        )

    assert result["logs"] == []
    assert result["total"] == 0
