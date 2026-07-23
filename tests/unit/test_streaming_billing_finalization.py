import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import routstr.auth as auth_module
from routstr.auth import (
    ReservationSnapshot,
    adjust_payment_for_tokens,
    get_reservation_snapshot,
    pay_for_request,
    release_reservation,
)
from routstr.core.db import ApiKey, ReservationRelease
from routstr.payment.cost_calculation import MaxCostData
from routstr.upstream.base import BaseUpstreamProvider


async def _engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    return engine


@pytest.mark.asyncio
async def test_release_reservation_is_durable_and_idempotent() -> None:
    engine = await _engine()
    key = ApiKey(hashed_key="key", balance=1_000)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()
        await pay_for_request(key, 500, session)
        snapshot = await get_reservation_snapshot(key, session)

        record = await session.get(ReservationRelease, snapshot.release_id)
        assert record is not None and record.status == "active"
        assert await release_reservation(snapshot, session, 500) is True
        assert await release_reservation(snapshot, session, 500) is True

        await session.refresh(key)
        await session.refresh(record)
        assert key.reserved_balance == 0
        assert key.reserved_at is None
        assert record.status == "released"
    await engine.dispose()


@pytest.mark.asyncio
async def test_release_only_owns_its_concurrent_reservation() -> None:
    engine = await _engine()
    key = ApiKey(hashed_key="key", balance=1_000)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()

        await pay_for_request(key, 400, session)
        first = await get_reservation_snapshot(key, session)
        await pay_for_request(key, 400, session)
        second = await get_reservation_snapshot(key, session)

        assert first.release_id != second.release_id
        assert await release_reservation(first, session, 400) is True
        assert await release_reservation(first, session, 400) is True
        await session.refresh(key)
        assert key.reserved_balance == 400

        assert await release_reservation(second, session, 400) is True
        await session.refresh(key)
        assert key.reserved_balance == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_release_updates_parent_and_child_atomically() -> None:
    engine = await _engine()
    parent = ApiKey(hashed_key="parent", balance=1_000)
    child = ApiKey(hashed_key="child", parent_key_hash="parent", balance=0)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()
        await pay_for_request(child, 500, session)
        snapshot = await get_reservation_snapshot(child, session)

        assert await release_reservation(snapshot, session, 500) is True
        await session.refresh(parent)
        await session.refresh(child)
        assert (parent.reserved_balance, child.reserved_balance) == (0, 0)
        assert (parent.reserved_at, child.reserved_at) == (None, None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_release_rolls_back_partial_parent_child_update() -> None:
    engine = await _engine()
    parent = ApiKey(hashed_key="parent", balance=1_000)
    child = ApiKey(hashed_key="child", parent_key_hash="parent", balance=0)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()
        await pay_for_request(child, 500, session)
        snapshot = await get_reservation_snapshot(child, session)
        child.reserved_balance = 100
        session.add(child)
        await session.commit()

        assert await release_reservation(snapshot, session, 500) is False
        await session.refresh(parent)
        await session.refresh(child)
        record = await session.get(ReservationRelease, snapshot.release_id)
        assert (parent.reserved_balance, child.reserved_balance) == (500, 100)
        assert record is not None and record.status == "active"
    await engine.dispose()


@pytest.mark.asyncio
async def test_post_commit_failure_cannot_release_charged_reservation() -> None:
    engine = await _engine()
    key = ApiKey(hashed_key="key", balance=1_000)
    cost = MaxCostData(
        base_msats=500,
        input_msats=0,
        output_msats=0,
        total_msats=500,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()
        await pay_for_request(key, 500, session)
        snapshot = await get_reservation_snapshot(key, session)

        with (
            patch("routstr.auth.calculate_cost", AsyncMock(return_value=cost)),
            patch.object(
                session,
                "refresh",
                AsyncMock(side_effect=SQLAlchemyError("post-commit refresh failed")),
            ),
        ):
            with pytest.raises(SQLAlchemyError, match="post-commit refresh failed"):
                await adjust_payment_for_tokens(key, {}, session, 500)

        await session.rollback()
        assert await release_reservation(snapshot, session, 500) is False
        charged_key = await session.get(ApiKey, "key")
        record = await session.get(ReservationRelease, snapshot.release_id)
        assert charged_key is not None
        assert (charged_key.balance, charged_key.reserved_balance) == (500, 0)
        assert record is not None and record.status == "charged"
    await engine.dispose()


@pytest.mark.asyncio
async def test_generic_background_settlement_uses_explicit_reservation() -> None:
    engine = await _engine()
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test-key", provider_fee=1.0
    )
    key = ApiKey(hashed_key="generic-key", balance=1_000)
    cost = MaxCostData(
        base_msats=500,
        input_msats=0,
        output_msats=0,
        total_msats=500,
    )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()
        await pay_for_request(key, 500, session)
        snapshot = await get_reservation_snapshot(key, session)

    context_token = auth_module._current_reservation.set(None)
    try:
        with (
            patch(
                "routstr.upstream.base.create_session",
                side_effect=lambda: AsyncSession(engine, expire_on_commit=False),
            ),
            patch(
                "routstr.upstream.base.adjust_payment_for_tokens",
                auth_module.adjust_payment_for_tokens,
            ),
            patch("routstr.auth.calculate_cost", AsyncMock(return_value=cost)),
        ):
            await provider._finalize_generic_streaming_payment(
                key.hashed_key,
                500,
                "audio/speech",
                model_obj=None,
                provider_fee=provider.provider_fee,
                reservation_snapshot=snapshot,
            )
    finally:
        auth_module._current_reservation.reset(context_token)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        settled_key = await session.get(ApiKey, key.hashed_key)
        record = await session.get(ReservationRelease, snapshot.release_id)
        assert settled_key is not None
        assert (settled_key.balance, settled_key.reserved_balance) == (500, 0)
        assert record is not None and record.status == "charged"
    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_release_is_terminal_and_suppresses_background_charge() -> None:
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
    reservation_snapshot.reserved_msats = 500
    background_tasks = MagicMock()

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
            background_tasks=background_tasks,
        )

        with pytest.raises(SQLAlchemyError, match="database unavailable"):
            async for _ in response.body_iterator:
                pass

    session.rollback.assert_awaited_once()
    release.assert_awaited_once_with(reservation_snapshot, session, 500)
    background_tasks.add_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "release_outcome",
    [True, False, RuntimeError("release failed"), asyncio.CancelledError()],
)
async def test_responses_streaming_releases_and_raises_on_billing_failure(
    release_outcome: bool | BaseException,
) -> None:
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test-key"
    )

    async def aiter_bytes() -> AsyncGenerator[bytes, None]:
        yield (
            b'data: {"type":"response.completed","response":{"model":"test",'
            b'"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    upstream_response = MagicMock(
        status_code=200,
        headers={"content-type": "text/event-stream"},
    )
    upstream_response.aiter_bytes = aiter_bytes
    key = MagicMock(spec=ApiKey)
    key.hashed_key = "responses-key"
    session = MagicMock()
    session.get = AsyncMock(return_value=key)
    session.rollback = AsyncMock()
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    snapshot = ReservationSnapshot(
        release_id="responses-release",
        key_hash=key.hashed_key,
        billing_key_hash=key.hashed_key,
        reserved_msats=500,
    )
    release = (
        AsyncMock(side_effect=release_outcome)
        if isinstance(release_outcome, BaseException)
        else AsyncMock(return_value=release_outcome)
    )
    adjust = AsyncMock(side_effect=SQLAlchemyError("database unavailable"))

    with (
        patch("routstr.upstream.base.adjust_payment_for_tokens", adjust),
        patch("routstr.upstream.base.release_reservation", release),
        patch("routstr.upstream.base.create_session", return_value=session_context),
    ):
        response = await provider.handle_streaming_responses_completion(
            response=upstream_response,
            key=key,
            max_cost_for_model=500,
            reservation_snapshot=snapshot,
        )
        emitted = bytearray()
        with pytest.raises(SQLAlchemyError, match="database unavailable"):
            async for chunk in response.body_iterator:
                if isinstance(chunk, str):
                    emitted.extend(chunk.encode())
                else:
                    emitted.extend(bytes(chunk))

    assert b'"total_msats": 0' not in emitted
    adjust.assert_awaited_once()
    session.rollback.assert_awaited_once()
    release.assert_awaited_once_with(snapshot, session, 500)


@pytest.mark.asyncio
@pytest.mark.parametrize("via_litellm", [False, True])
@pytest.mark.parametrize(
    "release_outcome",
    [True, False, RuntimeError("release failed"), asyncio.CancelledError()],
)
async def test_messages_streaming_releases_and_raises_on_billing_failure(
    via_litellm: bool,
    release_outcome: bool | BaseException,
) -> None:
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test-key"
    )
    key = MagicMock(spec=ApiKey)
    key.hashed_key = "messages-key"
    session = MagicMock()
    session.get = AsyncMock(return_value=key)
    session.rollback = AsyncMock()
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    snapshot = ReservationSnapshot(
        release_id=f"messages-{'litellm' if via_litellm else 'native'}",
        key_hash=key.hashed_key,
        billing_key_hash=key.hashed_key,
        reserved_msats=500,
    )
    release = (
        AsyncMock(side_effect=release_outcome)
        if isinstance(release_outcome, BaseException)
        else AsyncMock(return_value=release_outcome)
    )
    adjust = AsyncMock(side_effect=SQLAlchemyError("database unavailable"))

    async def native_chunks() -> AsyncGenerator[bytes, None]:
        yield (
            b'event: message_start\ndata: {"type":"message_start","message":'
            b'{"model":"test","usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
        )
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    async def litellm_chunks() -> AsyncGenerator[dict, None]:
        yield {
            "type": "message_start",
            "message": {
                "model": "test",
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        }
        yield {"type": "message_stop"}

    with (
        patch("routstr.upstream.base.adjust_payment_for_tokens", adjust),
        patch("routstr.upstream.base.release_reservation", release),
        patch("routstr.upstream.base.create_session", return_value=session_context),
    ):
        if via_litellm:
            response = provider._stream_litellm_messages(
                iterator=litellm_chunks(),
                key=key,
                max_cost_for_model=500,
                requested_model=None,
                reservation_snapshot=snapshot,
            )
        else:
            upstream_response = MagicMock(
                status_code=200,
                headers={"content-type": "text/event-stream"},
            )
            upstream_response.aiter_bytes = native_chunks
            response = await provider.handle_streaming_messages_completion(
                response=upstream_response,
                key=key,
                max_cost_for_model=500,
                reservation_snapshot=snapshot,
            )

        with pytest.raises(SQLAlchemyError, match="database unavailable"):
            async for _ in response.body_iterator:
                pass

    adjust.assert_awaited_once()
    session.rollback.assert_awaited_once()
    release.assert_awaited_once_with(snapshot, session, 500)


@pytest.mark.asyncio
async def test_cross_key_reservation_snapshot_is_rejected_without_mutation() -> None:
    engine = await _engine()
    first = ApiKey(hashed_key="first", balance=1_000)
    second = ApiKey(hashed_key="second", balance=1_000)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(first)
        session.add(second)
        await session.commit()
        await pay_for_request(first, 500, session)
        snapshot = await get_reservation_snapshot(first, session)

        with pytest.raises(RuntimeError, match="does not belong"):
            await adjust_payment_for_tokens(
                second,
                {"model": "test", "usage": None},
                session,
                500,
                reservation_snapshot=snapshot,
            )

        await session.refresh(first)
        await session.refresh(second)
        assert first.reserved_balance == 500
        assert second.reserved_balance == 0

    await engine.dispose()
