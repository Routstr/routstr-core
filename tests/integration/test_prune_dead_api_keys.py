"""
Tests for prune_dead_api_keys — the janitor that removes provably-dead 0/0/0
API keys (funded keys fully refunded/expired without ever being used, plus bare
orphans), while protecting keys that are still meaningful.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from sqlalchemy.sql.dml import Update
from sqlmodel import col, update

from routstr.core.db import (
    ApiKey,
    CashuTransaction,
    LightningInvoice,
    create_session,
    prune_dead_api_keys,
)

OLD = 100  # min_age_seconds used by the tests
NOW = int(time.time())
LONG_AGO = NOW - 10_000  # well past the grace period


async def _exists(key_hash: str) -> bool:
    async with create_session() as session:
        return (await session.get(ApiKey, key_hash)) is not None


def _dead_key(created_at: int | None) -> ApiKey:
    return ApiKey(
        hashed_key=f"dead_{uuid.uuid4().hex}",
        balance=0,
        reserved_balance=0,
        total_spent=0,
        total_requests=0,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_prunes_old_refunded_zero_key(patched_db_engine: None) -> None:
    """A funded-then-refunded key (0/0/0, NULL parent, old) is pruned."""
    key = _dead_key(LONG_AGO)
    async with create_session() as session:
        session.add(key)
        await session.commit()

    async with create_session() as session:
        pruned = await prune_dead_api_keys(session, OLD)

    assert pruned == 1
    assert not await _exists(key.hashed_key)


@pytest.mark.asyncio
async def test_grace_period_protects_fresh_key(patched_db_engine: None) -> None:
    """A dead-looking but recently created key is protected by the grace period."""
    key = _dead_key(int(time.time()))
    async with create_session() as session:
        session.add(key)
        await session.commit()

    async with create_session() as session:
        pruned = await prune_dead_api_keys(session, OLD)

    assert pruned == 0
    assert await _exists(key.hashed_key)


@pytest.mark.asyncio
async def test_used_key_never_pruned(patched_db_engine: None) -> None:
    """Keys with any spend/requests or live balance are never pruned."""
    spent = _dead_key(LONG_AGO)
    spent.total_spent = 1
    requested = _dead_key(LONG_AGO)
    requested.total_requests = 1
    funded = _dead_key(LONG_AGO)
    funded.balance = 1000
    reserved = _dead_key(LONG_AGO)
    reserved.reserved_balance = 500

    async with create_session() as session:
        for k in (spent, requested, funded, reserved):
            session.add(k)
        await session.commit()

    async with create_session() as session:
        pruned = await prune_dead_api_keys(session, OLD)

    assert pruned == 0
    for k in (spent, requested, funded, reserved):
        assert await _exists(k.hashed_key)


@pytest.mark.asyncio
async def test_parent_and_child_keys_are_not_pruned(
    patched_db_engine: None,
) -> None:
    """Pruning must not orphan child keys or delete valid children."""
    parent = _dead_key(LONG_AGO)
    child = ApiKey(
        hashed_key=f"child_{uuid.uuid4().hex}",
        balance=0,
        reserved_balance=0,
        total_spent=0,
        total_requests=0,
        created_at=LONG_AGO,
        parent_key_hash=parent.hashed_key,
    )
    async with create_session() as session:
        session.add(parent)
        session.add(child)
        await session.commit()

    async with create_session() as session:
        pruned = await prune_dead_api_keys(session, OLD)

    assert pruned == 0
    assert await _exists(parent.hashed_key)
    assert await _exists(child.hashed_key)


@pytest.mark.asyncio
async def test_pending_invoice_protects_key(patched_db_engine: None) -> None:
    """A key referenced by a pending topup invoice is never pruned mid-topup."""
    key = _dead_key(LONG_AGO)
    invoice = LightningInvoice(
        id=f"inv_{uuid.uuid4().hex}",
        bolt11=f"lnbc_{uuid.uuid4().hex}",
        amount_sats=10,
        description="topup",
        payment_hash=uuid.uuid4().hex,
        status="pending",
        api_key_hash=key.hashed_key,
        purpose="topup",
        expires_at=NOW + 10_000,
    )
    async with create_session() as session:
        session.add(key)
        session.add(invoice)
        await session.commit()

    async with create_session() as session:
        pruned = await prune_dead_api_keys(session, OLD)

    assert pruned == 0
    assert await _exists(key.hashed_key)


@pytest.mark.asyncio
async def test_paid_invoice_does_not_protect_key(patched_db_engine: None) -> None:
    """A settled (non-pending) invoice does not keep a dead key alive."""
    key = _dead_key(LONG_AGO)
    invoice = LightningInvoice(
        id=f"inv_{uuid.uuid4().hex}",
        bolt11=f"lnbc_{uuid.uuid4().hex}",
        amount_sats=10,
        description="topup",
        payment_hash=uuid.uuid4().hex,
        status="paid",
        api_key_hash=key.hashed_key,
        purpose="topup",
        expires_at=NOW - 1,
    )
    async with create_session() as session:
        session.add(key)
        session.add(invoice)
        await session.commit()

    async with create_session() as session:
        pruned = await prune_dead_api_keys(session, OLD)

    assert pruned == 1
    assert not await _exists(key.hashed_key)


@pytest.mark.asyncio
async def test_key_that_becomes_meaningful_during_prune_survives(
    patched_db_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revalidate before unlink/delete so a late top-up cannot be pruned."""
    key = _dead_key(LONG_AGO)
    txn = CashuTransaction(
        id=uuid.uuid4().hex,
        token="cashuABC",
        amount=21,
        unit="sat",
        type="in",
        source="apikey",
        api_key_hashed_key=key.hashed_key,
    )
    async with create_session() as session:
        session.add(key)
        session.add(txn)
        await session.commit()

    async with create_session() as session:
        original_exec = cast(Callable[..., Awaitable[Any]], session.exec)
        topped_up = False

        async def exec_with_late_topup(
            statement: Any, *args: Any, **kwargs: Any
        ) -> Any:
            nonlocal topped_up
            if (
                not topped_up
                and isinstance(statement, Update)
                and getattr(statement.table, "name", None) == "cashu_transactions"
            ):
                topped_up = True
                async with create_session() as topup_session:
                    await topup_session.exec(  # type: ignore[call-overload]
                        update(ApiKey)
                        .where(col(ApiKey.hashed_key) == key.hashed_key)
                        .values(balance=42)
                    )
                    await topup_session.commit()
            return await original_exec(statement, *args, **kwargs)

        monkeypatch.setattr(session, "exec", exec_with_late_topup)
        pruned = await prune_dead_api_keys(session, OLD)

    assert pruned == 0
    assert await _exists(key.hashed_key)
    async with create_session() as session:
        surviving = await session.get(CashuTransaction, txn.id)
        assert surviving is not None
        assert surviving.api_key_hashed_key == key.hashed_key


@pytest.mark.asyncio
async def test_transaction_audit_trail_preserved(patched_db_engine: None) -> None:
    """Pruning a refunded key keeps its cashu_transactions, unlinked from the key."""
    key = _dead_key(LONG_AGO)
    txn = CashuTransaction(
        id=uuid.uuid4().hex,
        token="cashuABC",
        amount=21,
        unit="sat",
        type="in",
        source="apikey",
        api_key_hashed_key=key.hashed_key,
    )
    async with create_session() as session:
        session.add(key)
        session.add(txn)
        await session.commit()

    async with create_session() as session:
        pruned = await prune_dead_api_keys(session, OLD)

    assert pruned == 1
    assert not await _exists(key.hashed_key)

    async with create_session() as session:
        surviving = await session.get(CashuTransaction, txn.id)
        assert surviving is not None, "Financial audit row must survive key deletion"
        assert surviving.api_key_hashed_key is None, "Link must be nulled, not dangling"
        assert surviving.amount == 21


@pytest.mark.asyncio
async def test_periodic_prune_disabled_returns_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-positive intervals disable the janitor."""
    from unittest.mock import AsyncMock

    from routstr import auth
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "dead_key_prune_interval_seconds", 0)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(auth.asyncio, "sleep", sleep_mock)

    await auth.periodic_dead_key_prune()

    sleep_mock.assert_not_called()
