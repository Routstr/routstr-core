from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.auth import get_reservation_snapshot, release_reservation
from routstr.core.db import ApiKey
from routstr.upstream.base import BaseUpstreamProvider


@pytest.mark.asyncio
async def test_release_reservation_clears_reserved_balance() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    key = ApiKey(
        hashed_key="key", balance=1_000, reserved_balance=500, reserved_at=123
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()

        snapshot = await get_reservation_snapshot(key, session)
        assert await release_reservation(snapshot, session, 500) is True
        await session.refresh(key)
        assert key.reserved_balance == 0
        assert key.reserved_at is None
        assert await release_reservation(snapshot, session, 500) is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_release_reservation_preserves_other_concurrent_reservations() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    key = ApiKey(
        hashed_key="key", balance=1_000, reserved_balance=800, reserved_at=123
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()

        first_snapshot = await get_reservation_snapshot(key, session)
        second_snapshot = await get_reservation_snapshot(key, session)
        assert await release_reservation(first_snapshot, session, 400) is True
        assert await release_reservation(second_snapshot, session, 400) is True
        await session.refresh(key)
        assert key.reserved_balance == 0
        assert key.reserved_at is None
        assert await release_reservation(first_snapshot, session, 400) is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_release_reservation_updates_parent_and_child_atomically() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    parent = ApiKey(
        hashed_key="parent", balance=1_000, reserved_balance=500, reserved_at=123
    )
    child = ApiKey(
        hashed_key="child",
        parent_key_hash="parent",
        balance=0,
        reserved_balance=500,
        reserved_at=123,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()

        snapshot = await get_reservation_snapshot(child, session)
        assert await release_reservation(snapshot, session, 500) is True
        await session.refresh(parent)
        await session.refresh(child)
        assert (parent.reserved_balance, child.reserved_balance) == (0, 0)
        assert (parent.reserved_at, child.reserved_at) == (None, None)

    await engine.dispose()


@pytest.mark.asyncio
async def test_release_reservation_rolls_back_partial_parent_child_update() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    parent = ApiKey(hashed_key="parent", balance=1_000, reserved_balance=500)
    child = ApiKey(
        hashed_key="child",
        parent_key_hash="parent",
        balance=0,
        reserved_balance=100,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()

        snapshot = await get_reservation_snapshot(child, session)
        assert await release_reservation(snapshot, session, 500) is False
        await session.refresh(parent)
        await session.refresh(child)
        assert (parent.reserved_balance, child.reserved_balance) == (500, 100)

    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_billing_error_releases_reservation_and_propagates() -> None:
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test-key"
    )

    async def aiter_bytes() -> AsyncGenerator[bytes, None]:
        yield b"data: [DONE]\n\n"

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.headers = {"content-type": "text/event-stream"}
    upstream_response.aiter_bytes = aiter_bytes

    key = MagicMock(spec=ApiKey)
    key.hashed_key = "test-key-hash"
    session = MagicMock()
    session.get = AsyncMock(return_value=key)
    session.rollback = AsyncMock()
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    release = AsyncMock(return_value=True)
    reservation_snapshot = MagicMock()

    with (
        patch(
            "routstr.upstream.base.adjust_payment_for_tokens",
            AsyncMock(side_effect=SQLAlchemyError("database unavailable")),
        ),
        patch(
            "routstr.upstream.base.get_reservation_snapshot",
            AsyncMock(return_value=reservation_snapshot),
        ),
        patch("routstr.upstream.base.release_reservation", release),
        patch("routstr.upstream.base.create_session", return_value=session_context),
    ):
        response = await provider.handle_streaming_chat_completion(
            response=upstream_response,
            key=key,
            max_cost_for_model=500,
            background_tasks=MagicMock(),
        )

        with pytest.raises(SQLAlchemyError, match="database unavailable"):
            async for _ in response.body_iterator:
                pass

    session.rollback.assert_awaited_once()
    release.assert_awaited_once_with(reservation_snapshot, session, 500)
