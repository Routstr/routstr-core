"""Covers the SQLite busy-timeout configuration on the async engine.

Ensures the engine waits for the single SQLite write lock instead of raising
SQLITE_BUSY immediately, and that the value is shared so test engines reproduce
the same lock-contention behaviour as production.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from routstr.core.db import SQLITE_BUSY_TIMEOUT_SECONDS, create_db_engine


@pytest.mark.asyncio
async def test_engine_applies_shared_busy_timeout() -> None:
    engine = create_db_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA busy_timeout"))
            busy_timeout_ms = result.scalar()
    finally:
        await engine.dispose()

    assert busy_timeout_ms == SQLITE_BUSY_TIMEOUT_SECONDS * 1000
