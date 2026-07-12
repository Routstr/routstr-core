from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from routstr.core import db


@pytest.mark.asyncio
async def test_store_cashu_transaction_retries_transient_database_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert = AsyncMock(
        side_effect=[
            OperationalError("insert", {}, Exception("database is locked")),
            None,
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(db, "_insert_cashu_transaction", insert)
    monkeypatch.setattr(db.asyncio, "sleep", sleep)

    stored = await db.store_cashu_transaction(
        token="cashuAretry", amount=100, unit="sat", typ="out"
    )

    assert stored is True
    assert insert.await_count == 2
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
async def test_store_cashu_transaction_stops_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert = AsyncMock(
        side_effect=OperationalError("insert", {}, Exception("database is locked"))
    )
    sleep = AsyncMock()
    critical = AsyncMock()
    monkeypatch.setattr(db, "_insert_cashu_transaction", insert)
    monkeypatch.setattr(db.asyncio, "sleep", sleep)
    monkeypatch.setattr(db.logger, "critical", critical)

    stored = await db.store_cashu_transaction(
        token="cashuAfailed", amount=100, unit="sat", typ="out"
    )

    assert stored is False
    assert insert.await_count == 3
    assert sleep.await_count == 2
    critical.assert_called_once()
    assert critical.call_args.kwargs["extra"]["token"] == "cashuAfailed"


@pytest.mark.asyncio
async def test_store_cashu_transaction_treats_duplicate_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert = AsyncMock(
        side_effect=IntegrityError("insert", {}, Exception("unique constraint"))
    )
    exists = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "_insert_cashu_transaction", insert)
    monkeypatch.setattr(db, "_cashu_transaction_exists", exists)

    stored = await db.store_cashu_transaction(
        token="cashuAduplicate", amount=100, unit="sat", typ="out"
    )

    assert stored is True
    insert.assert_awaited_once()
    exists.assert_awaited_once_with("cashuAduplicate", "out")
