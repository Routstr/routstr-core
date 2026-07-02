"""Covers that the integration engine reproduces production's SQLite busy timeout.

The concurrent-reservation tests rely on the test engine contending on the write
lock the same way production does; this guards against the fixture silently
drifting back to sqlite3's shorter default.
"""

from typing import Any

import pytest
from sqlalchemy import text

from routstr.core.db import SQLITE_BUSY_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_integration_engine_matches_production_busy_timeout(
    integration_engine: Any,
) -> None:
    async with integration_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA busy_timeout"))
        busy_timeout_ms = result.scalar()

    assert busy_timeout_ms == SQLITE_BUSY_TIMEOUT_SECONDS * 1000
