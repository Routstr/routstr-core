import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import httpx
import pytest

import routstr.upstream.http_client as http_client_module
from routstr.core.settings import settings
from routstr.upstream.http_client import (
    close_upstream_http_client,
    get_upstream_http_client,
    upstream_origin_key,
)


@pytest.mark.asyncio
async def test_upstream_http_client_is_reused_until_shutdown() -> None:
    first = get_upstream_http_client("https://api.example.com/v1/chat")
    second = get_upstream_http_client("https://api.example.com/v1/models")

    assert second is first
    assert not first.is_closed

    await close_upstream_http_client()
    assert first.is_closed

    replacement = get_upstream_http_client("https://api.example.com/v1/chat")
    try:
        assert replacement is not first
        assert not replacement.is_closed
    finally:
        await close_upstream_http_client()


@pytest.mark.asyncio
async def test_upstream_http_client_is_isolated_per_origin() -> None:
    try:
        first = get_upstream_http_client("https://one.example.com/v1/chat")
        second = get_upstream_http_client("https://two.example.com/v1/chat")
        other_port = get_upstream_http_client("https://one.example.com:8443/v1/chat")

        assert first is not second
        assert first is not other_port
    finally:
        await close_upstream_http_client()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.example.com/v1/chat?x=1", "https://api.example.com"),
        ("HTTPS://API.EXAMPLE.COM:443/v1/chat", "https://api.example.com"),
        ("http://API.EXAMPLE.COM:80/v1/chat", "http://api.example.com"),
        ("http://api.example.com:8080/v1/chat", "http://api.example.com:8080"),
        ("https://bücher.example/v1/chat", "https://xn--bcher-kva.example"),
        ("https://xn--bcher-kva.example/v1/chat", "https://xn--bcher-kva.example"),
        ("https://[2001:db8::1]/v1/chat", "https://[2001:db8::1]"),
        (
            "https://[2001:0DB8:0:0:0:0:0:1]:443/v1/chat",
            "https://[2001:db8::1]",
        ),
        ("https://[2001:db8::1]:8443/v1/chat", "https://[2001:db8::1]:8443"),
    ],
)
def test_upstream_origin_key_returns_http_origin(url: str, expected: str) -> None:
    assert upstream_origin_key(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "/v1/chat",
        "ftp://api.example.com",
        "https://:443",
        "https://user@",
        "https://example.com:",
        "https://example.com:not-a-port",
        "https://example.com:65536",
        "https://[2001:db8::1",
        "https://exa mple.com",
        "https://user@example.com",
        "https://user:secret@example.com",
        "https://:secret@example.com",
        "https://@example.com",
        None,
    ],
)
def test_upstream_origin_key_rejects_invalid_urls(url: object) -> None:
    with pytest.raises(ValueError, match="absolute HTTP") as exc_info:
        upstream_origin_key(url)  # type: ignore[arg-type]
    assert "secret" not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_url", "second_url"),
    [
        ("https://EXAMPLE.com:443/v1/chat", "https://example.com/v1/models"),
        (
            "https://bücher.example/v1/chat",
            "https://xn--bcher-kva.example/v1/models",
        ),
    ],
)
async def test_equivalent_origins_share_one_client(
    first_url: str, second_url: str
) -> None:
    try:
        first = get_upstream_http_client(first_url)
        second = get_upstream_http_client(second_url)
        assert second is first
    finally:
        await close_upstream_http_client()


@pytest.mark.asyncio
async def test_upstream_http_client_applies_configured_pool_bounds() -> None:
    with (
        patch.object(
            http_client_module.httpx,
            "Limits",
            wraps=httpx.Limits,
        ) as build_limits,
        patch.object(
            http_client_module.httpx,
            "AsyncHTTPTransport",
            wraps=httpx.AsyncHTTPTransport,
        ) as build_transport,
    ):
        client = get_upstream_http_client("https://api.example.com")

    try:
        assert client.timeout.pool == settings.upstream_pool_timeout
        assert client.timeout.read == settings.upstream_read_timeout
        assert client.timeout.connect == settings.upstream_connect_timeout
        assert client.timeout.write == settings.upstream_write_timeout
        build_limits.assert_called_once_with(
            max_connections=settings.upstream_max_connections,
            max_keepalive_connections=settings.upstream_max_keepalive_connections,
            keepalive_expiry=settings.upstream_keepalive_expiry,
        )
        build_transport.assert_called_once()
        assert (
            build_transport.call_args.kwargs["retries"]
            == settings.upstream_connect_retries
        )
    finally:
        await close_upstream_http_client()


@pytest.mark.asyncio
async def test_upstream_http_client_does_not_share_cookies() -> None:
    client = get_upstream_http_client("https://example.com")
    try:
        first = client.build_request("GET", "https://example.com/test")
        response = httpx.Response(
            200,
            headers={"set-cookie": "sticky=upstream; Path=/"},
            request=first,
        )
        client.cookies.extract_cookies(response)

        later = client.build_request("GET", "https://example.com/test")
        explicit = client.build_request(
            "GET", "https://example.com/test", headers={"cookie": "user=provided"}
        )

        assert "cookie" not in later.headers
        assert explicit.headers["cookie"] == "user=provided"
    finally:
        await close_upstream_http_client()


@pytest.mark.asyncio
async def test_shutdown_closes_foreign_client_on_its_owner_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign_loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    close_finished = threading.Event()
    close_loops: list[asyncio.AbstractEventLoop] = []

    def run_foreign_loop() -> None:
        asyncio.set_event_loop(foreign_loop)
        loop_ready.set()
        foreign_loop.run_forever()

    thread = threading.Thread(target=run_foreign_loop)
    thread.start()
    assert loop_ready.wait(timeout=10)

    async def make_client() -> httpx.AsyncClient:
        client = get_upstream_http_client("https://example.com")
        original_close = client.aclose

        async def tracked_close() -> None:
            close_loops.append(asyncio.get_running_loop())
            await original_close()
            close_finished.set()

        monkeypatch.setattr(client, "aclose", tracked_close)
        return client

    client_future = asyncio.run_coroutine_threadsafe(make_client(), foreign_loop)
    client = await asyncio.to_thread(client_future.result, 10)
    try:
        await close_upstream_http_client()
        assert await asyncio.to_thread(close_finished.wait, 10)
        assert client.is_closed
        assert close_loops == [foreign_loop]

        await close_upstream_http_client()
        assert not http_client_module._pending_closes
    finally:
        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        await asyncio.to_thread(thread.join, 10)
        assert not thread.is_alive()
        foreign_loop.close()


@pytest.mark.asyncio
async def test_shutdown_rehomes_queued_close_when_owner_loop_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign_loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    blocker_started = threading.Event()
    allow_stop = threading.Event()

    def run_foreign_loop() -> None:
        asyncio.set_event_loop(foreign_loop)
        loop_ready.set()
        foreign_loop.run_forever()

    thread = threading.Thread(target=run_foreign_loop)
    thread.start()
    assert loop_ready.wait(timeout=10)

    async def make_client() -> httpx.AsyncClient:
        return get_upstream_http_client("https://example.com")

    client_future = asyncio.run_coroutine_threadsafe(make_client(), foreign_loop)
    client = await asyncio.to_thread(client_future.result, 10)
    original_close = client.aclose
    close_loops: list[asyncio.AbstractEventLoop] = []

    async def tracked_close() -> None:
        close_loops.append(asyncio.get_running_loop())
        await original_close()

    monkeypatch.setattr(client, "aclose", tracked_close)

    def stop_before_next_iteration() -> None:
        blocker_started.set()
        assert allow_stop.wait(timeout=10)
        foreign_loop.stop()

    foreign_loop.call_soon_threadsafe(stop_before_next_iteration)
    assert blocker_started.wait(timeout=10)

    try:
        closing = asyncio.create_task(close_upstream_http_client())
        while not http_client_module._pending_closes:
            await asyncio.sleep(0)
        assert not client.is_closed

        allow_stop.set()
        await asyncio.to_thread(thread.join, 10)
        assert not thread.is_alive()

        await closing
        assert client.is_closed
        assert close_loops == [asyncio.get_running_loop()]
        assert not http_client_module._pending_closes
    finally:
        allow_stop.set()
        if thread.is_alive():
            foreign_loop.call_soon_threadsafe(foreign_loop.stop)
            await asyncio.to_thread(thread.join, 10)
        foreign_loop.close()


@pytest.mark.asyncio
async def test_shutdown_finishes_transport_close_on_stopped_owner_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign_loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    transport_started = threading.Event()
    allow_transport_close = threading.Event()
    transport_finished = threading.Event()

    class BlockingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

        async def aclose(self) -> None:
            transport_started.set()
            while not allow_transport_close.is_set():
                await asyncio.sleep(0)
            transport_finished.set()

    client = httpx.AsyncClient(transport=BlockingTransport())
    monkeypatch.setattr(http_client_module, "_build_client", lambda: client)

    def run_foreign_loop() -> None:
        asyncio.set_event_loop(foreign_loop)
        loop_ready.set()
        foreign_loop.run_forever()

    thread = threading.Thread(target=run_foreign_loop)
    thread.start()
    assert loop_ready.wait(timeout=10)

    async def register_client() -> None:
        assert get_upstream_http_client("https://example.com") is client

    registered = asyncio.run_coroutine_threadsafe(register_client(), foreign_loop)
    await asyncio.to_thread(registered.result, 10)

    try:
        closing = asyncio.create_task(close_upstream_http_client())
        assert await asyncio.to_thread(transport_started.wait, 10)

        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        await asyncio.to_thread(thread.join, 10)
        assert not thread.is_alive()

        allow_transport_close.set()
        await closing

        assert transport_finished.is_set()
        assert client.is_closed
        assert not http_client_module._pending_closes
    finally:
        allow_transport_close.set()
        if thread.is_alive():
            foreign_loop.call_soon_threadsafe(foreign_loop.stop)
            await asyncio.to_thread(thread.join, 10)
        if not foreign_loop.is_closed():
            foreign_loop.close()


@pytest.mark.asyncio
async def test_started_close_on_closed_owner_loop_retries_transport() -> None:
    class CountingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.close_count = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

        async def aclose(self) -> None:
            self.close_count += 1

    owner_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    owner_loop.is_closed.return_value = True
    owner_loop.is_running.return_value = False
    transport = CountingTransport()
    client = httpx.AsyncClient(transport=transport)
    completion: concurrent.futures.Future[None] = concurrent.futures.Future()
    completion.set_running_or_notify_cancel()
    task = MagicMock(spec=asyncio.Task)
    task.done.return_value = False
    submission = http_client_module._CloseSubmission(
        client=client,
        completion=completion,
        task=task,
    )
    http_client_module._pending_closes[owner_loop] = {completion: submission}

    http_client_module._collect_completed_closes()
    await http_client_module._drain_pending_closes()

    assert submission.retired
    assert transport.close_count == 1
    assert client.is_closed
    assert not http_client_module._pending_closes


@pytest.mark.asyncio
async def test_shutdown_closes_client_after_owner_loop_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[httpx.AsyncClient] = []
    owner_loops: list[asyncio.AbstractEventLoop] = []

    def create_on_stopped_loop() -> None:
        owner_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(owner_loop)
        owner_loops.append(owner_loop)

        async def make_client() -> None:
            created.append(get_upstream_http_client("https://example.com"))

        owner_loop.run_until_complete(make_client())

    thread = threading.Thread(target=create_on_stopped_loop)
    thread.start()
    await asyncio.to_thread(thread.join, 10)
    assert not thread.is_alive()

    client = created[0]
    owner_loop = owner_loops[0]
    close_loops: list[asyncio.AbstractEventLoop] = []
    original_close = client.aclose

    async def tracked_close() -> None:
        close_loops.append(asyncio.get_running_loop())
        await original_close()

    monkeypatch.setattr(client, "aclose", tracked_close)
    try:
        await close_upstream_http_client()
        assert client.is_closed
        assert close_loops == [asyncio.get_running_loop()]
        assert not http_client_module._pending_closes
    finally:
        owner_loop.close()


@pytest.mark.asyncio
async def test_shutdown_closes_client_after_owner_loop_closed() -> None:
    created: list[httpx.AsyncClient] = []

    def create_and_close_loop() -> None:
        owner_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(owner_loop)

        async def make_client() -> None:
            created.append(get_upstream_http_client("https://example.com"))

        owner_loop.run_until_complete(make_client())
        owner_loop.close()

    thread = threading.Thread(target=create_and_close_loop)
    thread.start()
    await asyncio.to_thread(thread.join, 10)
    assert not thread.is_alive()

    client = created[0]
    await close_upstream_http_client()
    assert client.is_closed


@pytest.mark.asyncio
async def test_shutdown_retries_failed_client_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = get_upstream_http_client("https://example.com")
    original_close = client.aclose
    attempts = 0

    async def flaky_close() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("close failed")
        await original_close()

    monkeypatch.setattr(client, "aclose", flaky_close)

    await close_upstream_http_client()
    assert not client.is_closed
    assert any(
        client in failed for failed in http_client_module._failed_closes.values()
    )

    await close_upstream_http_client()
    assert client.is_closed
    assert attempts == 2
    assert not http_client_module._failed_closes


@pytest.mark.asyncio
async def test_shutdown_prunes_externally_closed_failed_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = get_upstream_http_client("https://example.com")
    original_close = client.aclose

    async def fail_close() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(client, "aclose", fail_close)
    await close_upstream_http_client()
    assert http_client_module._failed_closes

    await original_close()
    await close_upstream_http_client()

    assert client.is_closed
    assert not http_client_module._failed_closes
    assert not http_client_module._pending_closes


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_shutdown_retries_transport_after_httpx_marks_client_closed(
    failure: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailOnceTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.attempts = 0
            self.completed = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

        async def aclose(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise failure
            self.completed = True

    transport = FailOnceTransport()
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(http_client_module, "_build_client", lambda: client)
    assert get_upstream_http_client("https://example.com") is client

    await close_upstream_http_client()
    assert client.is_closed
    assert transport.attempts == 1
    assert not transport.completed
    assert any(
        client in failed for failed in http_client_module._failed_closes.values()
    )

    await close_upstream_http_client()
    assert transport.attempts == 2
    assert transport.completed
    assert not http_client_module._failed_closes
    assert not http_client_module._pending_closes


@pytest.mark.asyncio
async def test_shutdown_collects_done_task_before_owner_loop_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign_loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    transport_finished = threading.Event()

    class StopAfterCloseTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

        async def aclose(self) -> None:
            transport_finished.set()
            asyncio.get_running_loop().stop()

    client = httpx.AsyncClient(transport=StopAfterCloseTransport())
    monkeypatch.setattr(http_client_module, "_build_client", lambda: client)

    def run_foreign_loop() -> None:
        asyncio.set_event_loop(foreign_loop)
        loop_ready.set()
        foreign_loop.run_forever()

    thread = threading.Thread(target=run_foreign_loop)
    thread.start()
    assert loop_ready.wait(timeout=10)

    async def register_client() -> None:
        assert get_upstream_http_client("https://example.com") is client

    registered_client = asyncio.run_coroutine_threadsafe(
        register_client(), foreign_loop
    )
    await asyncio.to_thread(registered_client.result, 10)

    try:
        await asyncio.wait_for(close_upstream_http_client(), timeout=1)
        assert transport_finished.is_set()
        assert client.is_closed
        assert not http_client_module._pending_closes
        assert not http_client_module._failed_closes
    finally:
        if thread.is_alive():
            foreign_loop.call_soon_threadsafe(foreign_loop.stop)
            await asyncio.to_thread(thread.join, 10)
        foreign_loop.close()


@pytest.mark.asyncio
async def test_close_submission_settlement_is_atomic_across_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = asyncio.create_task(asyncio.sleep(0))
    await task

    client = httpx.AsyncClient()
    completion: concurrent.futures.Future[None] = concurrent.futures.Future()
    completion.set_running_or_notify_cancel()
    submission = http_client_module._CloseSubmission(
        client=client,
        completion=completion,
        task=task,
    )
    loop = asyncio.get_running_loop()
    http_client_module._pending_closes[loop] = {completion: submission}

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    original_settle = http_client_module._settle_close_submission

    def synchronized_settle(
        close_submission: http_client_module._CloseSubmission,
        completed: asyncio.Task[None],
    ) -> None:
        barrier.wait(timeout=10)
        original_settle(close_submission, completed)

    def run(action: Callable[[], None]) -> None:
        try:
            action()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(
        http_client_module,
        "_settle_close_submission",
        synchronized_settle,
    )
    collector = threading.Thread(
        target=run,
        args=(lambda: http_client_module._settle_submission_from_task(submission),),
    )
    callback = threading.Thread(
        target=run,
        args=(lambda: http_client_module._finish_close_submission(submission, task),),
    )

    try:
        collector.start()
        callback.start()
        collector.join(timeout=10)
        callback.join(timeout=10)

        assert not collector.is_alive()
        assert not callback.is_alive()
        assert errors == []
        assert completion.result() is None

        monkeypatch.setattr(
            http_client_module,
            "_settle_close_submission",
            original_settle,
        )
        http_client_module._collect_completed_closes()

        assert http_client_module._close_completed.get(client) is True
        assert not http_client_module._pending_closes
        assert not http_client_module._failed_closes
    finally:
        http_client_module._pending_closes.pop(loop, None)
        http_client_module._close_completed.pop(client, None)
        await client.aclose()


@pytest.mark.asyncio
async def test_close_submission_rejects_conflicting_outcomes() -> None:
    succeeded = asyncio.create_task(asyncio.sleep(0))

    async def fail() -> None:
        raise RuntimeError("different outcome")

    failed = asyncio.create_task(fail())
    await succeeded
    with pytest.raises(RuntimeError, match="different outcome"):
        await failed

    client = httpx.AsyncClient()
    completion: concurrent.futures.Future[None] = concurrent.futures.Future()
    completion.set_running_or_notify_cancel()
    submission = http_client_module._CloseSubmission(client, completion)

    try:
        http_client_module._settle_close_submission(submission, succeeded)
        with pytest.raises(RuntimeError, match="conflicting outcomes"):
            http_client_module._settle_close_submission(submission, failed)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_upstream_http_client_cannot_reopen_during_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = get_upstream_http_client("https://example.com")
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    original_close = client.aclose

    async def delayed_close() -> None:
        close_started.set()
        await allow_close.wait()
        await original_close()

    monkeypatch.setattr(client, "aclose", delayed_close)
    closing = asyncio.create_task(close_upstream_http_client())
    await close_started.wait()

    with pytest.raises(RuntimeError, match="shutting down"):
        get_upstream_http_client("https://example.com")

    allow_close.set()
    await closing
