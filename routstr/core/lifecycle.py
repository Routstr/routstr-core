"""Supervise the real downstream connection, outside HTTP middleware wrappers."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import get_logger
from .settings import settings

logger = get_logger(__name__)


class DownstreamTerminated(OSError):
    """Raised by downstream_send after disconnect; expected, not a server error."""


@dataclass
class RequestLifetime:
    deadline: float = 0
    stopped: bool = False


request_lifetime: ContextVar[RequestLifetime | None] = ContextVar(
    "request_lifetime", default=None
)


class RequestLifecycleMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        lifetime = RequestLifetime(
            deadline=asyncio.get_running_loop().time()
            + settings.max_request_lifetime_seconds
        )
        token = request_lifetime.set(lifetime)
        disconnected = asyncio.Event()
        # One receive consumer. Backpressure uploads until consumed; after the
        # final body message, continue listening independently of the app.
        messages: asyncio.Queue[Message] = asyncio.Queue(maxsize=1)
        response_started = False

        async def pump() -> None:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    return
                await messages.put(message)

        async def downstream_receive() -> Message:
            if disconnected.is_set():
                return {"type": "http.disconnect"}
            get = asyncio.create_task(messages.get())
            gone = asyncio.create_task(disconnected.wait())
            try:
                await asyncio.wait((get, gone), return_when=asyncio.FIRST_COMPLETED)
                if disconnected.is_set():
                    return {"type": "http.disconnect"}
                return get.result()
            finally:
                for task in (get, gone):
                    task.cancel()
                await asyncio.gather(get, gone, return_exceptions=True)

        async def downstream_send(message: Message) -> None:
            nonlocal response_started
            if disconnected.is_set() or lifetime.stopped:
                raise DownstreamTerminated("Downstream request terminated")
            async with asyncio.timeout(settings.downstream_send_timeout_seconds):
                await send(message)
            if message["type"] == "http.response.start":
                response_started = True

        receiver = asyncio.create_task(pump())
        work: asyncio.Future[None] = asyncio.ensure_future(
            self.app(scope, downstream_receive, downstream_send)
        )
        gone = asyncio.create_task(disconnected.wait())
        timed_out = False
        try:
            done, _ = await asyncio.wait(
                (work, gone),
                timeout=settings.max_request_lifetime_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if gone in done and not response_started and work not in done:
                # A pre-response wallet or billing operation may have accepted
                # funds already. Let it reach its own settlement before closing.
                done, _ = await asyncio.wait(
                    (work,),
                    timeout=max(0, lifetime.deadline - asyncio.get_running_loop().time()),
                )
            if work in done:
                try:
                    await work
                except DownstreamTerminated:
                    if not disconnected.is_set():
                        raise
                    logger.debug("Client disconnected before response completed")
            elif not disconnected.is_set():
                timed_out = True
        finally:
            lifetime.stopped = True
            for task in (receiver, gone, work):
                task.cancel()
            # Detached stream finalizers own settlement. The heartbeat stops
            # with the request; durable expiry recovers any abandoned row.
            done, pending = await asyncio.wait(
                (receiver, gone, work), timeout=settings.request_cleanup_timeout_seconds
            )
            for task in done:
                if not task.cancelled():
                    task.exception()
            for task in pending:
                task.cancel()
                task.add_done_callback(
                    lambda t: t.exception() if not t.cancelled() else None
                )
            request_lifetime.reset(token)
        if timed_out and work.done() and not disconnected.is_set() and not response_started:
            async with asyncio.timeout(settings.downstream_send_timeout_seconds):
                await send({"type": "http.response.start", "status": 504, "headers": []})
                await send(
                    {"type": "http.response.body", "body": b"Request deadline exceeded"}
                )
