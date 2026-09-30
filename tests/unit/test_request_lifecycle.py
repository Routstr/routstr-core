import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

import routstr.core.db as db_module
from routstr.auth import (
    ReservationSnapshot,
    _claim_reservation_for_charge,
    _stop_reservation_heartbeat,
    pay_for_request,
)
from routstr.core.db import ApiKey, ReservationRelease
from routstr.core.lifecycle import RequestLifecycleMiddleware
from routstr.core.middleware import LoggingMiddleware
from routstr.core.settings import settings


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disconnect", "deadline", "send"])
async def test_lifecycle_stops_live_work(reason: str) -> None:
    closed = asyncio.Event()
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()
    await receive_queue.put({"type": "http.request", "body": b"", "more_body": False})
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            assert (await receive())["type"] == "http.request"
            await send({"type": "http.response.start", "status": 200, "headers": []})
            while True:
                await send(
                    {"type": "http.response.body", "body": b"x", "more_body": True}
                )
                await asyncio.sleep(0.01)
        finally:
            closed.set()

    async def send(message: Message) -> None:
        sent.append(message)
        if reason == "send" and message["type"] == "http.response.body":
            await asyncio.sleep(100)

    async def disconnect() -> None:
        await asyncio.sleep(0.02)
        await receive_queue.put({"type": "http.disconnect"})

    task = asyncio.create_task(disconnect()) if reason == "disconnect" else None
    with (
        patch.object(settings, "max_request_lifetime_seconds", 0.08),
        patch.object(settings, "downstream_send_timeout_seconds", 0.03),
        patch.object(settings, "request_cleanup_timeout_seconds", 0.1),
    ):
        try:
            await asyncio.wait_for(
                RequestLifecycleMiddleware(app)(
                    {"type": "http"}, receive_queue.get, send
                ),
                1,
            )
        except TimeoutError:
            assert reason == "send"
    if task:
        await task
    assert closed.is_set()
    assert sent


@pytest.mark.asyncio
async def test_unrelated_oserror_still_propagates() -> None:
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()
    await receive_queue.put({"type": "http.request", "body": b"", "more_body": False})

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        raise OSError("Connection reset by peer")

    async def send(message: Message) -> None:
        pass

    with pytest.raises(OSError, match="Connection reset by peer"):
        await asyncio.wait_for(
            RequestLifecycleMiddleware(app)({"type": "http"}, receive_queue.get, send),
            1,
        )


@pytest.mark.asyncio
async def test_disconnect_before_headers_preserves_wallet_work() -> None:
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()
    await receive_queue.put({"type": "http.request", "body": b"", "more_body": False})
    entered = asyncio.Event()
    finish_wallet = asyncio.Event()
    wallet_credited = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        entered.set()
        await finish_wallet.wait()  # The mint accepted the token; credit is still pending.
        wallet_credited.set()
        await send({"type": "http.response.start", "status": 200, "headers": []})

    run = asyncio.create_task(
        RequestLifecycleMiddleware(app)(
            {"type": "http"}, receive_queue.get, lambda message: asyncio.sleep(0)
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    await receive_queue.put({"type": "http.disconnect"})
    await asyncio.sleep(0.02)
    assert not run.done()
    finish_wallet.set()
    await asyncio.wait_for(run, 1)  # No propagated exception for an expected disconnect.
    assert wallet_credited.is_set()


@pytest.mark.asyncio
async def test_disconnect_before_headers_with_logging_middleware() -> None:
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()
    await receive_queue.put({"type": "http.request", "body": b"", "more_body": False})
    entered = asyncio.Event()
    finish_wallet = asyncio.Event()
    wallet_credited = asyncio.Event()

    async def wallet(request: Request) -> PlainTextResponse:
        await request.body()
        entered.set()
        await finish_wallet.wait()
        wallet_credited.set()
        return PlainTextResponse("settled")

    app = RequestLifecycleMiddleware(
        LoggingMiddleware(Starlette(routes=[Route("/wallet", wallet, methods=["POST"])]))
    )
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/wallet",
        "raw_path": b"/wallet",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1234),
        "server": ("test", 80),
    }

    async def send(message: Message) -> None:
        pass

    run = asyncio.create_task(app(scope, receive_queue.get, send))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await receive_queue.put({"type": "http.disconnect"})
        await asyncio.sleep(0.02)
        assert not run.done()
        finish_wallet.set()
        await asyncio.wait_for(run, 1)  # No propagated exception for an expected disconnect.
        assert wallet_credited.is_set()
    finally:
        finish_wallet.set()
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
async def test_deadline_cancels_app_before_sending_504() -> None:
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()
    await receive_queue.put({"type": "http.request", "body": b"", "more_body": False})
    sent: list[Message] = []
    app_stopped = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        try:
            await asyncio.sleep(100)
        finally:
            with pytest.raises(OSError, match="Downstream request terminated"):
                await send({"type": "http.response.start", "status": 200, "headers": []})
            app_stopped.set()

    async def send(message: Message) -> None:
        assert app_stopped.is_set()
        sent.append(message)

    with (
        patch.object(settings, "max_request_lifetime_seconds", 0.02),
        patch.object(settings, "request_cleanup_timeout_seconds", 0.1),
    ):
        await asyncio.wait_for(
            RequestLifecycleMiddleware(app)({"type": "http"}, receive_queue.get, send),
            1,
        )
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    assert sent[0]["status"] == 504


@pytest.mark.asyncio
async def test_disconnect_does_not_release_before_stream_settles(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'reservations.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def session() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db

    with patch.object(db_module, "create_session", session):
        async with session() as db:
            db.add(ApiKey(hashed_key="stream-key", balance=10_000))
            await db.commit()
        started = asyncio.Event()
        finalizer_started = asyncio.Event()
        settle = asyncio.Event()
        result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        snapshot: ReservationSnapshot | None = None
        receive_queue: asyncio.Queue[Message] = asyncio.Queue()
        await receive_queue.put({"type": "http.request", "body": b"", "more_body": False})

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            nonlocal snapshot
            async with session() as db:
                key = await db.get(ApiKey, "stream-key")
                assert key is not None
                snapshot = await pay_for_request(key, 1000, db)
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            started.set()
            try:
                await asyncio.sleep(100)
            finally:
                async def finalize() -> None:
                    assert snapshot is not None
                    finalizer_started.set()
                    await settle.wait()
                    async with session() as db:
                        claimed = await _claim_reservation_for_charge(snapshot, db)
                        await db.commit()
                    await _stop_reservation_heartbeat(snapshot.release_id)
                    result.set_result(claimed)

                asyncio.create_task(finalize())

        run = asyncio.create_task(
            RequestLifecycleMiddleware(app)(
                {"type": "http"}, receive_queue.get, lambda message: asyncio.sleep(0)
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            await receive_queue.put({"type": "http.disconnect"})
            await asyncio.wait_for(finalizer_started.wait(), 1)
            await asyncio.wait_for(run, 1)
            settle.set()
            assert await asyncio.wait_for(result, 1)
            assert snapshot is not None
            async with session() as db:
                row = await db.get(ReservationRelease, snapshot.release_id)
                assert row is not None and row.status == "charged"
        finally:
            settle.set()
            if snapshot is not None:
                await _stop_reservation_heartbeat(snapshot.release_id)
            await engine.dispose()
