"""Tests for stale reserved_balance handling (issue #551).

Covers:
- pay_for_request stamping reserved_at on billing and child keys
- release_stale_reservations sweeper semantics
- reset_all_reserved_balances clearing reserved_at
- refund endpoint self-healing stale/legacy reservations
- proxy reverting the reservation when the client disconnects (CancelledError)
"""

import asyncio
import time
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.auth import pay_for_request
from routstr.balance import refund_wallet_endpoint
from routstr.core.db import (
    ApiKey,
    release_stale_reservations,
    reset_all_reserved_balances,
)


def _make_engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
async def session() -> "AsyncGenerator[AsyncSession, None]":
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    db_session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db_session
    finally:
        await db_session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# pay_for_request stamps reserved_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pay_for_request_sets_reserved_at(session: AsyncSession) -> None:
    key = ApiKey(hashed_key="paykey", balance=10_000)
    session.add(key)
    await session.commit()

    before = int(time.time())
    await pay_for_request(key, 1_000, session)

    await session.refresh(key)
    assert key.reserved_balance == 1_000
    assert key.reserved_at is not None
    assert key.reserved_at >= before


@pytest.mark.asyncio
async def test_pay_for_request_sets_reserved_at_on_child_key(session: AsyncSession) -> None:
    parent = ApiKey(hashed_key="parentkey", balance=10_000)
    child = ApiKey(hashed_key="childkey", balance=0, parent_key_hash="parentkey")
    session.add(parent)
    session.add(child)
    await session.commit()

    await pay_for_request(child, 1_000, session)

    await session.refresh(parent)
    await session.refresh(child)
    assert parent.reserved_balance == 1_000
    assert parent.reserved_at is not None
    assert child.reserved_balance == 1_000
    assert child.reserved_at is not None


@pytest.mark.asyncio
async def test_revert_clears_reserved_at_when_fully_released(
    session: AsyncSession,
) -> None:
    from routstr.auth import revert_pay_for_request

    key = ApiKey(hashed_key="revertkey", balance=10_000)
    session.add(key)
    await session.commit()

    await pay_for_request(key, 1_000, session)
    reverted = await revert_pay_for_request(key, session, 1_000)

    assert reverted is True
    await session.refresh(key)
    assert key.reserved_balance == 0
    assert key.reserved_at is None


@pytest.mark.asyncio
async def test_revert_keeps_reserved_at_while_other_reservations_remain(
    session: AsyncSession,
) -> None:
    from routstr.auth import revert_pay_for_request

    key = ApiKey(hashed_key="partialrevert", balance=10_000)
    session.add(key)
    await session.commit()

    await pay_for_request(key, 1_000, session)
    await pay_for_request(key, 1_000, session)
    reverted = await revert_pay_for_request(key, session, 1_000)

    assert reverted is True
    await session.refresh(key)
    assert key.reserved_balance == 1_000
    assert key.reserved_at is not None


# ---------------------------------------------------------------------------
# release_stale_reservations sweeper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_stale_reservations_releases_old(session: AsyncSession) -> None:
    key = ApiKey(
        hashed_key="stalekey",
        balance=5_000,
        reserved_balance=1_000,
        reserved_at=int(time.time()) - 1_000,
    )
    session.add(key)
    await session.commit()

    released = await release_stale_reservations(session, max_age_seconds=300)

    assert released == 1
    await session.refresh(key)
    assert key.reserved_balance == 0
    assert key.reserved_at is None


@pytest.mark.asyncio
async def test_release_stale_reservations_keeps_fresh(session: AsyncSession) -> None:
    key = ApiKey(
        hashed_key="freshkey",
        balance=5_000,
        reserved_balance=1_000,
        reserved_at=int(time.time()),
    )
    session.add(key)
    await session.commit()

    released = await release_stale_reservations(session, max_age_seconds=300)

    assert released == 0
    await session.refresh(key)
    assert key.reserved_balance == 1_000
    assert key.reserved_at is not None


@pytest.mark.asyncio
async def test_release_stale_reservations_skips_null_reserved_at(session: AsyncSession) -> None:
    # Reservations without a timestamp may belong to instances running older
    # code (rolling deploy) — the background sweeper must not touch them.
    key = ApiKey(
        hashed_key="legacykey",
        balance=5_000,
        reserved_balance=1_000,
        reserved_at=None,
    )
    session.add(key)
    await session.commit()

    released = await release_stale_reservations(session, max_age_seconds=300)

    assert released == 0
    await session.refresh(key)
    assert key.reserved_balance == 1_000


@pytest.mark.asyncio
async def test_reset_all_reserved_balances_clears_reserved_at(session: AsyncSession) -> None:
    key = ApiKey(
        hashed_key="resetkey",
        balance=5_000,
        reserved_balance=1_000,
        reserved_at=int(time.time()),
    )
    session.add(key)
    await session.commit()

    await reset_all_reserved_balances(session)

    await session.refresh(key)
    assert key.reserved_balance == 0
    assert key.reserved_at is None


# ---------------------------------------------------------------------------
# Refund endpoint self-healing
# ---------------------------------------------------------------------------


def _refund_patches(refund_token: str = "cashuArefund"):  # type: ignore[no-untyped-def]
    return (
        patch("routstr.balance.send_token", AsyncMock(return_value=refund_token)),
        patch("routstr.balance.store_cashu_transaction", AsyncMock()),
        patch("routstr.balance._refund_cache_get", AsyncMock(return_value=None)),
        patch("routstr.balance._refund_cache_set", AsyncMock()),
    )


async def _add_key(session: AsyncSession, **kwargs) -> ApiKey:  # type: ignore[no-untyped-def]
    key = ApiKey(refund_currency="sat", **kwargs)
    session.add(key)
    await session.commit()
    return key


@pytest.mark.asyncio
async def test_refund_self_heals_stale_reservation(session: AsyncSession) -> None:
    key = await _add_key(
        session,
        hashed_key="stalerefund",
        balance=5_000,
        reserved_balance=2_000,
        reserved_at=int(time.time()) - 10_000,
    )

    p1, p2, p3, p4 = _refund_patches()
    with p1, p2, p3, p4:
        result = await refund_wallet_endpoint(
            authorization="Bearer sk-stalerefund",
            x_cashu=None,
            session=session,
        )

    assert isinstance(result, dict)
    assert result["token"] == "cashuArefund"
    # Full balance refunded (5000 msats -> 5 sats), reservation healed
    assert result["sats"] == "5"
    await session.refresh(key)
    assert key.balance == 0
    assert key.reserved_balance == 0
    assert key.reserved_at is None


@pytest.mark.asyncio
async def test_refund_self_heals_legacy_null_reserved_at(session: AsyncSession) -> None:
    # Keys stuck from before reserved_at existed must be refundable.
    key = await _add_key(
        session,
        hashed_key="legacyrefund",
        balance=5_000,
        reserved_balance=2_000,
        reserved_at=None,
    )

    p1, p2, p3, p4 = _refund_patches()
    with p1, p2, p3, p4:
        result = await refund_wallet_endpoint(
            authorization="Bearer sk-legacyrefund",
            x_cashu=None,
            session=session,
        )

    assert isinstance(result, dict)
    assert result["token"] == "cashuArefund"
    await session.refresh(key)
    assert key.balance == 0
    assert key.reserved_balance == 0


@pytest.mark.asyncio
async def test_refund_rejects_recent_reservation(session: AsyncSession) -> None:
    from fastapi import HTTPException

    await _add_key(
        session,
        hashed_key="activerefund",
        balance=5_000,
        reserved_balance=2_000,
        reserved_at=int(time.time()),
    )

    p1, p2, p3, p4 = _refund_patches()
    with p1, p2, p3, p4:
        with pytest.raises(HTTPException) as exc_info:
            await refund_wallet_endpoint(
                authorization="Bearer sk-activerefund",
                x_cashu=None,
                session=session,
            )

    assert exc_info.value.status_code == 400
    assert "ongoing requests" in exc_info.value.detail


@pytest.mark.asyncio
async def test_refund_without_reservation_still_works(session: AsyncSession) -> None:
    key = await _add_key(
        session,
        hashed_key="plainrefund",
        balance=5_000,
        reserved_balance=0,
    )

    p1, p2, p3, p4 = _refund_patches()
    with p1, p2, p3, p4:
        result = await refund_wallet_endpoint(
            authorization="Bearer sk-plainrefund",
            x_cashu=None,
            session=session,
        )

    assert isinstance(result, dict)
    assert result["token"] == "cashuArefund"
    await session.refresh(key)
    assert key.balance == 0


# ---------------------------------------------------------------------------
# Proxy reverts reservation on client disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_reverts_reservation_on_client_disconnect() -> None:
    from routstr import proxy as proxy_module

    key = ApiKey(hashed_key="cancelkey", balance=10_000)

    request = MagicMock()
    request.method = "POST"
    request.headers = {"authorization": "Bearer sk-cancelkey"}
    request.body = AsyncMock(return_value=b'{"model": "test-model"}')

    upstream = MagicMock()
    upstream.provider_type = "test"
    upstream.prepare_headers = MagicMock(side_effect=lambda h: h)
    upstream.forward_request = AsyncMock(side_effect=asyncio.CancelledError())

    session = MagicMock()
    revert_mock = AsyncMock(return_value=True)

    with (
        patch.object(proxy_module, "get_model_instance", return_value=MagicMock()),
        patch.object(proxy_module, "get_provider_for_model", return_value=[upstream]),
        patch.object(
            proxy_module, "get_max_cost_for_model", AsyncMock(return_value=1_000)
        ),
        patch.object(
            proxy_module,
            "calculate_discounted_max_cost",
            AsyncMock(return_value=1_000),
        ),
        patch.object(proxy_module, "check_token_balance", MagicMock()),
        patch.object(
            proxy_module, "get_bearer_token_key", AsyncMock(return_value=key)
        ),
        patch.object(proxy_module, "pay_for_request", AsyncMock(return_value=1_000)),
        patch.object(proxy_module, "revert_pay_for_request", revert_mock),
    ):
        with pytest.raises(asyncio.CancelledError):
            await proxy_module.proxy(request, "v1/chat/completions", session=session)

    revert_mock.assert_awaited_once_with(key, session, 1_000)
