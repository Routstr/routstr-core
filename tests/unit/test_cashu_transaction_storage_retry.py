from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core import db


@pytest.mark.asyncio
async def test_cashu_transaction_storage_retries_then_succeeds() -> None:
    store = AsyncMock(side_effect=[OSError("database locked"), True])
    sleep = AsyncMock()

    with (
        patch("routstr.core.db.store_cashu_transaction", store),
        patch("routstr.core.db.asyncio.sleep", sleep),
    ):
        stored = await db.store_cashu_transaction_with_retry(
            token="cashuAretry",
            amount=100,
            unit="sat",
        )

    assert stored is True
    assert store.await_count == 2
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
async def test_cashu_transaction_retry_is_idempotent_after_ambiguous_commit() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    original_store = db.store_cashu_transaction
    attempts = 0

    async def ambiguous_store(**kwargs: Any) -> bool:
        nonlocal attempts
        attempts += 1
        stored = await original_store(**kwargs)
        if attempts == 1:
            raise OSError("connection dropped after commit")
        return stored

    with (
        patch.object(db, "engine", engine),
        patch("routstr.core.db.store_cashu_transaction", ambiguous_store),
        patch("routstr.core.db.asyncio.sleep", AsyncMock()),
    ):
        stored = await db.store_cashu_transaction_with_retry(
            token="cashuAambiguous",
            amount=100,
            unit="sat",
        )

        async with AsyncSession(engine) as session:
            result = await session.exec(select(db.CashuTransaction))
            transactions = result.all()

    assert stored is True
    assert attempts == 2
    assert len(transactions) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_cashu_transaction_storage_raises_after_bounded_retries() -> None:
    error = OSError("database unavailable")
    store = AsyncMock(side_effect=error)
    sleep = AsyncMock()

    with (
        patch("routstr.core.db.store_cashu_transaction", store),
        patch("routstr.core.db.asyncio.sleep", sleep),
        patch("routstr.core.db.logger.critical") as critical,
    ):
        with pytest.raises(OSError, match="database unavailable"):
            await db.store_cashu_transaction_with_retry(
                token="cashuAfail",
                amount=100,
                unit="sat",
                max_attempts=3,
            )

    assert store.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]
    critical.assert_called_once()
