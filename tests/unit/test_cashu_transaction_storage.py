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
    critical.assert_called_once_with(
        "Cashu transaction could not be stored",
        extra={
            "error_type": "Exception",
            "type": "out",
            "request_id": None,
            "amount": 100,
            "unit": "sat",
            "mint_url": None,
            "attempts_performed": 3,
            "max_attempts": 3,
        },
    )
    assert "cashuAfailed" not in repr(critical.call_args)


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


@pytest.mark.asyncio
async def test_store_cashu_transaction_retries_duplicate_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert = AsyncMock(
        side_effect=IntegrityError("insert", {}, Exception("unique constraint"))
    )
    exists = AsyncMock(
        side_effect=[
            OperationalError("select", {}, Exception("database is locked")),
            True,
        ]
    )
    sleep = AsyncMock()
    warning = AsyncMock()
    monkeypatch.setattr(db, "_insert_cashu_transaction", insert)
    monkeypatch.setattr(db, "_cashu_transaction_exists", exists)
    monkeypatch.setattr(db.asyncio, "sleep", sleep)
    monkeypatch.setattr(db.logger, "warning", warning)

    stored = await db.store_cashu_transaction(
        token="cashuAlookup-retry", amount=100, unit="sat", typ="out"
    )

    assert stored is True
    assert insert.await_count == 2
    assert exists.await_count == 2
    sleep.assert_awaited_once_with(0.25)
    assert "cashuAlookup-retry" not in repr(warning.call_args)
    assert warning.call_args.kwargs["extra"]["error_type"] == "Exception"


@pytest.mark.asyncio
async def test_store_cashu_transaction_bounds_duplicate_lookup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert = AsyncMock(
        side_effect=IntegrityError("insert", {}, Exception("unique constraint"))
    )
    exists = AsyncMock(
        side_effect=OperationalError("select", {}, Exception("database is locked"))
    )
    sleep = AsyncMock()
    critical = AsyncMock()
    monkeypatch.setattr(db, "_insert_cashu_transaction", insert)
    monkeypatch.setattr(db, "_cashu_transaction_exists", exists)
    monkeypatch.setattr(db.asyncio, "sleep", sleep)
    monkeypatch.setattr(db.logger, "critical", critical)

    stored = await db.store_cashu_transaction(
        token="cashuAlookup-failed", amount=100, unit="sat", typ="out"
    )

    assert stored is False
    assert insert.await_count == 3
    assert exists.await_count == 3
    assert sleep.await_count == 2
    critical.assert_called_once()
    assert "cashuAlookup-failed" not in repr(critical.call_args)


@pytest.mark.asyncio
async def test_store_cashu_transaction_contains_non_transient_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert = AsyncMock(
        side_effect=IntegrityError("insert", {}, Exception("unique constraint"))
    )
    exists = AsyncMock(side_effect=RuntimeError("lookup failed"))
    critical = AsyncMock()
    monkeypatch.setattr(db, "_insert_cashu_transaction", insert)
    monkeypatch.setattr(db, "_cashu_transaction_exists", exists)
    monkeypatch.setattr(db.logger, "critical", critical)

    stored = await db.store_cashu_transaction(
        token="cashuAlookup-error", amount=100, unit="sat", typ="out"
    )

    assert stored is False
    insert.assert_awaited_once()
    exists.assert_awaited_once()
    critical.assert_called_once_with(
        "Cashu transaction could not be stored",
        extra={
            "error_type": "RuntimeError",
            "type": "out",
            "request_id": None,
            "amount": 100,
            "unit": "sat",
            "mint_url": None,
            "attempts_performed": 1,
            "max_attempts": 3,
        },
    )
