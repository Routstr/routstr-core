import asyncio
import json
from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import BackgroundTasks
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
from routstr.payment.models import Architecture, Model, Pricing
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
async def test_release_clears_reservation_aggregates_atomically() -> None:
    engine = await _engine()
    key = ApiKey(hashed_key="key", balance=1_000)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()
        await pay_for_request(key, 500, session)
        snapshot = await get_reservation_snapshot(key, session)

        assert await release_reservation(snapshot, session, 500) is True
        await session.refresh(key)
        assert (key.reserved_balance, key.reserved_at) == (0, None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_release_repairs_partial_aggregate_corruption() -> None:
    """An aggregate that no longer holds the reservation must not leave
    the durable row active forever: the release rolls the subtraction back and
    terminalizes the reservation without touching aggregates."""
    engine = await _engine()
    key = ApiKey(hashed_key="key", balance=1_000)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()
        await pay_for_request(key, 500, session)
        snapshot = await get_reservation_snapshot(key, session)
        key.reserved_balance = 100
        session.add(key)
        await session.commit()

        assert await release_reservation(snapshot, session, 500) is True
        await session.refresh(key)
        record = await session.get(ReservationRelease, snapshot.release_id)
        # Aggregates untouched — legacy cleanup reconciles them when stale.
        assert key.reserved_balance == 100
        assert record is not None and record.status == "released"
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
@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("finalization_fails", [False, True])
async def test_partial_remote_protocol_error_finalizes_and_closes_once(
    api: str,
    finalization_fails: bool,
) -> None:
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test-key"
    )

    async def aiter_bytes() -> AsyncGenerator[bytes, None]:
        yield b'data: {"model":"test","choices":[{"delta":{"content":"hi"}}]}\n\n'
        raise httpx.RemoteProtocolError("incomplete chunked read")

    upstream_response = MagicMock(
        status_code=200, headers={"content-type": "text/event-stream"}
    )
    upstream_response.aiter_bytes = aiter_bytes
    upstream_response.aclose = AsyncMock()
    client = MagicMock()
    client.aclose = AsyncMock()
    key = MagicMock(spec=ApiKey)
    key.hashed_key = f"{api}-partial"
    key.balance = 10_000
    session = MagicMock()
    session.get = AsyncMock(return_value=key)
    session.rollback = AsyncMock()
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    adjust = (
        AsyncMock(side_effect=SQLAlchemyError("database unavailable"))
        if finalization_fails
        else AsyncMock(return_value={"input_tokens": 0, "output_tokens": 0})
    )
    snapshot = ReservationSnapshot(
        release_id=f"{api}-partial-release",
        key_hash=key.hashed_key,
        billing_key_hash=key.hashed_key,
        reserved_msats=500,
    )
    release = AsyncMock(return_value=True)

    with (
        patch("routstr.upstream.base.adjust_payment_for_tokens", adjust),
        patch("routstr.upstream.base.release_reservation", release),
        patch("routstr.upstream.base.create_session", return_value=session_context),
    ):
        if api == "chat":
            response = await provider.handle_streaming_chat_completion(
                response=upstream_response,
                key=key,
                max_cost_for_model=500,
                background_tasks=BackgroundTasks(),
                reservation_snapshot=snapshot,
                client=client,
            )
        else:
            response = await provider.handle_streaming_responses_completion(
                response=upstream_response,
                key=key,
                max_cost_for_model=500,
                reservation_snapshot=snapshot,
                client=client,
            )
        emitted = bytearray()
        with pytest.raises(httpx.RemoteProtocolError):
            async for chunk in response.body_iterator:
                emitted.extend(
                    chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                )

    adjust.assert_awaited_once()
    if finalization_fails:
        session.rollback.assert_awaited_once()
        release.assert_awaited_once_with(snapshot, session, 500)
    else:
        release.assert_not_awaited()
    upstream_response.aclose.assert_awaited_once()
    client.aclose.assert_awaited_once()
    assert b"[DONE]" not in emitted


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_partial_stream_preserves_transport_error_when_billing_db_is_down(
    api: str,
) -> None:
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test-key"
    )

    async def aiter_bytes() -> AsyncGenerator[bytes, None]:
        yield b'data: {"model":"test","choices":[]}\n\n'
        raise httpx.RemoteProtocolError("incomplete chunked read")

    upstream_response = MagicMock(
        status_code=200, headers={"content-type": "text/event-stream"}
    )
    upstream_response.aiter_bytes = aiter_bytes
    upstream_response.aclose = AsyncMock()
    client = MagicMock()
    client.aclose = AsyncMock()
    key = MagicMock(spec=ApiKey)
    key.hashed_key = f"{api}-database-down"
    key.balance = 10_000
    snapshot = ReservationSnapshot(
        release_id=f"{api}-database-down-release",
        key_hash=key.hashed_key,
        billing_key_hash=key.hashed_key,
        reserved_msats=500,
    )
    unavailable_session = MagicMock()
    unavailable_session.__aenter__ = AsyncMock(
        side_effect=SQLAlchemyError("database unavailable")
    )
    unavailable_session.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "routstr.upstream.base.create_session", return_value=unavailable_session
    ):
        if api == "chat":
            response = await provider.handle_streaming_chat_completion(
                response=upstream_response,
                key=key,
                max_cost_for_model=500,
                background_tasks=BackgroundTasks(),
                reservation_snapshot=snapshot,
                client=client,
            )
        else:
            response = await provider.handle_streaming_responses_completion(
                response=upstream_response,
                key=key,
                max_cost_for_model=500,
                reservation_snapshot=snapshot,
                client=client,
            )
        with pytest.raises(httpx.RemoteProtocolError, match="incomplete chunked read"):
            async for _ in response.body_iterator:
                pass

    upstream_response.aclose.assert_awaited_once()
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_responses_streaming_duplicate_publishes_zero_settled_cost() -> None:
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test-key"
    )

    async def aiter_bytes() -> AsyncGenerator[bytes, None]:
        yield (
            b'data: {"type":"response.completed","response":{"model":"test",'
            b'"usage":{"input_tokens":2,"output_tokens":1}}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    upstream_response = MagicMock(
        status_code=200,
        headers={"content-type": "text/event-stream"},
    )
    upstream_response.aiter_bytes = aiter_bytes
    key = MagicMock(spec=ApiKey)
    key.hashed_key = "responses-duplicate"
    key.balance = 10_000
    session = MagicMock()
    session.get = AsyncMock(return_value=key)
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    cost_data = {
        "input_tokens": 2,
        "output_tokens": 1,
        "input_msats": 1_000,
        "output_msats": 500,
        "total_msats": 1_500,
        "charged_msats": 0,
        "total_usd": 0.0001,
    }

    with (
        patch(
            "routstr.upstream.base.adjust_payment_for_tokens",
            AsyncMock(return_value=cost_data),
        ),
        patch("routstr.upstream.base.create_session", return_value=session_context),
    ):
        response = await provider.handle_streaming_responses_completion(
            response=upstream_response,
            key=key,
            max_cost_for_model=500,
        )
        chunks = [
            chunk if isinstance(chunk, str) else bytes(chunk).decode()
            async for chunk in response.body_iterator
        ]

    completed = next(
        json.loads(line[6:])
        for line in "".join(chunks).splitlines()
        if line.startswith("data: {")
    )
    nested_usage = completed["response"]["usage"]
    assert nested_usage["cost_sats"] == 0
    assert nested_usage["cost"]["total_msats"] == 0
    assert nested_usage["cost"]["charged_msats"] == 0
    assert nested_usage["cost"]["computed_msats"] == 1_500
    assert completed["cost"]["total_msats"] == 0
    assert completed["cost"]["computed_msats"] == 1_500


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


@pytest.mark.asyncio
async def test_client_disconnect_midstream_estimates_usage_and_stops_heartbeat() -> (
    None
):
    """A client abort releases the hold after charging only estimated usage.

    Starlette closes the response generator (``aclose``) on disconnect. The
    finalizer still has the request and streamed deltas, so it can estimate
    usage without converting the reservation ceiling into the charge.
    """
    engine = await _engine()
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test-key", provider_fee=1.0
    )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = ApiKey(hashed_key="disconnect-key", balance=1_000)
        session.add(key)
        await session.commit()
        await pay_for_request(key, 500, session)
        snapshot = await get_reservation_snapshot(key, session)

    assert snapshot.release_id in auth_module._reservation_heartbeats

    async def aiter_bytes() -> AsyncGenerator[bytes, None]:
        # A live stream that never sends a usage chunk or [DONE]; the client
        # disconnects after the first delta.
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":" there"}}]}\n\n'

    upstream_response = MagicMock(
        status_code=200, headers={"content-type": "text/event-stream"}
    )
    upstream_response.aiter_bytes = aiter_bytes

    model = Model(
        id="test-model",
        name="test-model",
        created=0,
        description="",
        context_length=8_192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.01, completion=0.02),
        sats_pricing=Pricing(prompt=0.01, completion=0.02),
    )
    request_body = json.dumps(
        {"model": model.id, "messages": [{"role": "user", "content": "hi"}]}
    ).encode()

    background_tasks = BackgroundTasks()
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
            patch("routstr.upstream.count_tokens._count_with_litellm", return_value=3),
            patch(
                "routstr.upstream.count_tokens._count_text_with_litellm",
                return_value=2,
            ),
            patch(
                "routstr.payment.cost_calculation.sats_usd_price",
                return_value=5.0e-5,
            ),
        ):
            response = await provider.handle_streaming_chat_completion(
                response=upstream_response,
                key=key,
                max_cost_for_model=500,
                background_tasks=background_tasks,
                model_obj=model,
                reservation_snapshot=snapshot,
                request_body=request_body,
            )
            iterator = cast(AsyncGenerator[bytes, None], response.body_iterator)
            await iterator.__anext__()  # first chunk reaches the client
            await iterator.aclose()  # client aborts the socket here

            # Starlette runs the response's background tasks after the abort.
            for task in background_tasks.tasks:
                await task()
    finally:
        await auth_module._stop_reservation_heartbeat(snapshot.release_id)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        final_key = await session.get(ApiKey, "disconnect-key")
        record = await session.get(ReservationRelease, snapshot.release_id)

    assert final_key is not None
    # The reservation reached a single terminal outcome; funds are not locked.
    assert record is not None and record.status in {"charged", "released"}
    assert final_key.reserved_balance == 0
    # 3 input tokens × 10 msats + 2 output tokens × 20 msats = 70 msats.
    assert final_key.total_spent == 70
    assert final_key.balance == 930
    # The heartbeat is gone — no forever-renewing task on an abandoned request.
    assert snapshot.release_id not in auth_module._reservation_heartbeats
    await engine.dispose()
