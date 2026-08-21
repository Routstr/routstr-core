from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import _transaction_status, get_transactions_api
from routstr.core.db import CashuTransaction


@pytest.mark.asyncio
async def test_transactions_api_excludes_internal_sweep_claim_timestamp() -> None:
    transaction = CashuTransaction(
        token="cashu-token",
        amount=10,
        unit="sat",
        type="out",
        sweep_started_at=123,
    )
    count_result = MagicMock()
    count_result.one.return_value = 1
    transactions_result = MagicMock()
    transactions_result.all.return_value = [transaction]
    session = MagicMock()
    session.exec = AsyncMock(side_effect=[count_result, transactions_result])

    @asynccontextmanager
    async def create_session():  # type: ignore[no-untyped-def]
        yield session

    with patch("routstr.core.admin.create_session", create_session):
        response = await get_transactions_api()

    assert response["total"] == 1
    assert response["transactions"][0]["token"] == "cashu-token"
    assert "sweep_started_at" not in response["transactions"][0]


@pytest.mark.parametrize(
    ("source", "typ", "collected", "swept", "expected"),
    [
        ("admin", "out", False, False, "issued"),
        ("admin", "out", True, False, "collected"),
        ("admin", "out", False, True, "swept"),
        # The node redeems an incoming top-up itself, so it is never "issued".
        ("admin", "in", False, False, "pending"),
        ("admin", "in", True, False, "collected"),
        ("x-cashu", "out", False, False, "pending"),
        ("x-cashu", "out", True, False, "collected"),
        ("apikey", "out", False, True, "swept"),
    ],
)
def test_transaction_status(
    source: str, typ: str, collected: bool, swept: bool, expected: str
) -> None:
    transaction = CashuTransaction(
        token="t",
        amount=1,
        unit="sat",
        type=typ,
        source=source,
        collected=collected,
        swept=swept,
    )
    assert _transaction_status(transaction) == expected


@pytest.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


ROWS = [
    ("issued-withdrawal", "admin", "out", False, False),
    ("collected-withdrawal", "admin", "out", True, False),
    ("swept-withdrawal", "admin", "out", False, True),
    ("incoming-topup", "admin", "in", False, False),
    ("pending-xcashu", "x-cashu", "out", False, False),
    ("collected-apikey", "apikey", "out", True, False),
    # Nothing stops both terminal flags being set, and the sweeper can reach
    # this state when a token it claimed turns out to be already spent.
    ("collected-and-swept", "x-cashu", "out", True, True),
]


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        session.add_all(
            [
                CashuTransaction(
                    id=row_id,
                    token=row_id,
                    amount=1,
                    unit="sat",
                    source=source,
                    type=typ,
                    collected=collected,
                    swept=swept,
                )
                for row_id, source, typ, collected, swept in ROWS
            ]
        )
        await session.commit()


async def _query(
    factory: async_sessionmaker[AsyncSession], **kwargs: object
) -> list[dict]:
    @asynccontextmanager
    async def create_session():  # type: ignore[no-untyped-def]
        async with factory() as session:
            yield session

    with patch("routstr.core.admin.create_session", create_session):
        response = await get_transactions_api(**kwargs)  # type: ignore[arg-type]
    return response["transactions"]


@pytest.mark.asyncio
async def test_response_carries_status_for_every_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory)

    by_id = {tx["id"]: tx["status"] for tx in await _query(session_factory)}

    assert by_id == {
        "issued-withdrawal": "issued",
        "collected-withdrawal": "collected",
        "swept-withdrawal": "swept",
        "incoming-topup": "pending",
        "pending-xcashu": "pending",
        "collected-apikey": "collected",
        "collected-and-swept": "swept",
    }


@pytest.mark.asyncio
async def test_status_filters_partition_rows_by_reported_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory)
    unfiltered = {tx["id"]: tx["status"] for tx in await _query(session_factory)}

    filtered: dict[str, str] = {}
    for status in ("issued", "collected", "swept", "pending"):
        for tx in await _query(session_factory, status=status):
            assert tx["status"] == status
            filtered[tx["id"]] = status

    assert filtered == unfiltered


@pytest.mark.asyncio
async def test_issued_filter_returns_only_outstanding_withdrawals(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory)

    rows = await _query(session_factory, status="issued")

    assert [tx["id"] for tx in rows] == ["issued-withdrawal"]


@pytest.mark.asyncio
async def test_pending_filter_excludes_issued_withdrawals(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory)

    rows = await _query(session_factory, status="pending")
    ids = {tx["id"] for tx in rows}

    # It is uncollected and unswept, so it matched "pending" before it had a
    # status of its own.
    assert "issued-withdrawal" not in ids
    assert ids == {"incoming-topup", "pending-xcashu"}
