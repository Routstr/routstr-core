import asyncio
from unittest.mock import patch

import pytest
from starlette.types import Message, Receive, Scope, Send

from routstr.core.lifecycle import RequestLifecycleMiddleware
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
