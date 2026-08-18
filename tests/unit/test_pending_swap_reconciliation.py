"""Reconciliation of interrupted cross-mint swaps (pending_swaps checkpoints)."""

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from cashu.core.base import MeltQuoteState
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr import wallet
from routstr.core import db

SOURCE_MINT = "https://source.mint.test"
DEST_MINT = "https://dest.mint.test"
KEY_HASH = "hash-abc123"


@asynccontextmanager
async def _null_guard() -> AsyncGenerator[None, None]:
    yield


async def _passthrough_mint_op(fn: Any, **_kwargs: Any) -> Any:
    return await fn()


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def patched_env(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    @asynccontextmanager
    async def _create_session() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(db, "create_session", _create_session)
    monkeypatch.setattr(wallet, "wallet_operation_guard", _null_guard)
    monkeypatch.setattr(wallet, "run_mint_operation", _passthrough_mint_op)

    stored_transactions: list[dict[str, Any]] = []

    async def _store_tx(**kwargs: Any) -> None:
        stored_transactions.append(kwargs)

    monkeypatch.setattr(wallet, "store_cashu_transaction", _store_tx)

    state: dict[str, Any] = {
        "melt_state": MeltQuoteState.paid,
        "mint_error": None,
        "mint_calls": [],
        "transactions": stored_transactions,
    }

    class FakeSourceWallet:
        async def get_melt_quote(self, quote_id: str) -> Any:
            return SimpleNamespace(state=state["melt_state"], quote=quote_id)

    class FakeDestWallet:
        async def mint(self, amount: int, quote_id: str | None = None) -> Any:
            if state["mint_error"] is not None:
                raise state["mint_error"]
            state["mint_calls"].append((amount, quote_id))
            return []

    async def _fake_get_wallet(mint_url: str, unit: str = "sat", **_kw: Any) -> Any:
        return FakeSourceWallet() if mint_url == SOURCE_MINT else FakeDestWallet()

    monkeypatch.setattr(wallet, "get_wallet", _fake_get_wallet)
    return state


def _pending_row(**overrides: Any) -> db.PendingSwap:
    defaults: dict[str, Any] = {
        "source_mint": SOURCE_MINT,
        "source_unit": "sat",
        "melt_quote_id": "melt-quote-1",
        "dest_mint": DEST_MINT,
        "dest_unit": "sat",
        "mint_quote_id": "mint-quote-1",
        "minted_amount": 90,
        "key_hashed_key": KEY_HASH,
        "token": "cashuAtest",
        "state": "pending",
        "created_at": int(time.time()) - 3600,
    }
    defaults.update(overrides)
    return db.PendingSwap(**defaults)


async def _seed(engine: AsyncEngine, *rows: Any) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        for row in rows:
            session.add(row)
        await session.commit()


async def _all_rows(engine: AsyncEngine) -> list[db.PendingSwap]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(db.PendingSwap))
        return list(result.all())


@pytest.mark.asyncio
async def test_paid_melt_mints_and_credits_existing_key(
    engine: AsyncEngine, patched_env: dict[str, Any]
) -> None:
    await _seed(
        engine,
        db.ApiKey(hashed_key=KEY_HASH, balance=1_000),
        _pending_row(),
    )

    await wallet.reconcile_pending_swaps_once()

    assert patched_env["mint_calls"] == [(90, "mint-quote-1")]
    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = await session.get(db.ApiKey, KEY_HASH)
        assert key is not None
        assert key.balance == 1_000 + 90_000  # 90 sat credited as msats
    assert await _all_rows(engine) == []
    assert len(patched_env["transactions"]) == 1
    assert patched_env["transactions"][0]["api_key_hashed_key"] == KEY_HASH


@pytest.mark.asyncio
async def test_paid_melt_recreates_rolled_back_key(
    engine: AsyncEngine, patched_env: dict[str, Any]
) -> None:
    await _seed(engine, _pending_row())

    await wallet.reconcile_pending_swaps_once()

    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = await session.get(db.ApiKey, KEY_HASH)
        assert key is not None
        assert key.balance == 90_000
        assert key.refund_mint_url == DEST_MINT
        assert key.refund_currency == "sat"
    assert await _all_rows(engine) == []


@pytest.mark.asyncio
async def test_old_unpaid_melt_drops_checkpoint_without_credit(
    engine: AsyncEngine, patched_env: dict[str, Any]
) -> None:
    patched_env["melt_state"] = MeltQuoteState.unpaid
    await _seed(
        engine,
        db.ApiKey(hashed_key=KEY_HASH, balance=1_000),
        _pending_row(),
    )

    await wallet.reconcile_pending_swaps_once()

    assert patched_env["mint_calls"] == []
    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = await session.get(db.ApiKey, KEY_HASH)
        assert key is not None
        assert key.balance == 1_000
    assert await _all_rows(engine) == []


@pytest.mark.asyncio
async def test_recent_unpaid_melt_keeps_waiting(
    engine: AsyncEngine, patched_env: dict[str, Any]
) -> None:
    patched_env["melt_state"] = MeltQuoteState.unpaid
    await _seed(engine, _pending_row(created_at=int(time.time()) - 300))

    await wallet.reconcile_pending_swaps_once()

    rows = await _all_rows(engine)
    assert len(rows) == 1
    assert rows[0].state == "pending"
    assert rows[0].attempts == 1


@pytest.mark.asyncio
async def test_pending_melt_state_only_bumps_attempts(
    engine: AsyncEngine, patched_env: dict[str, Any]
) -> None:
    patched_env["melt_state"] = MeltQuoteState.pending
    await _seed(engine, _pending_row())

    await wallet.reconcile_pending_swaps_once()

    rows = await _all_rows(engine)
    assert len(rows) == 1
    assert rows[0].state == "pending"
    assert rows[0].attempts == 1
    assert patched_env["mint_calls"] == []


@pytest.mark.asyncio
async def test_transient_mint_failure_retries_later(
    engine: AsyncEngine, patched_env: dict[str, Any]
) -> None:
    patched_env["mint_error"] = RuntimeError("mint briefly down")
    await _seed(engine, _pending_row(state="melt_confirmed"))

    await wallet.reconcile_pending_swaps_once()

    rows = await _all_rows(engine)
    assert len(rows) == 1
    assert rows[0].state == "melt_confirmed"
    assert rows[0].attempts == 1
    assert rows[0].last_error == "mint briefly down"


@pytest.mark.asyncio
async def test_already_issued_quote_parks_as_stale(
    engine: AsyncEngine, patched_env: dict[str, Any]
) -> None:
    patched_env["mint_error"] = RuntimeError("quote already issued")
    await _seed(engine, _pending_row(state="melt_confirmed"))

    await wallet.reconcile_pending_swaps_once()

    rows = await _all_rows(engine)
    assert len(rows) == 1
    assert rows[0].state == "stale"


@pytest.mark.asyncio
async def test_fresh_checkpoint_left_for_inline_swap(
    engine: AsyncEngine, patched_env: dict[str, Any]
) -> None:
    await _seed(engine, _pending_row(created_at=int(time.time())))

    await wallet.reconcile_pending_swaps_once()

    rows = await _all_rows(engine)
    assert len(rows) == 1
    assert rows[0].attempts == 0
    assert patched_env["mint_calls"] == []


@pytest.mark.asyncio
async def test_attempt_cap_parks_checkpoint_as_stale(
    engine: AsyncEngine, patched_env: dict[str, Any]
) -> None:
    patched_env["melt_state"] = MeltQuoteState.pending
    await _seed(
        engine,
        _pending_row(attempts=wallet._SWAP_RECONCILE_MAX_ATTEMPTS - 1),
    )

    await wallet.reconcile_pending_swaps_once()

    rows = await _all_rows(engine)
    assert len(rows) == 1
    assert rows[0].state == "stale"
