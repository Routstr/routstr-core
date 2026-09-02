"""Per-origin HTTP client pools with event-loop-aware shutdown."""

import asyncio
import concurrent.futures
import ipaddress
import threading
import weakref
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from ..core import get_logger
from ..core.settings import settings

logger = get_logger(__name__)

# Guards all module-level bookkeeping (_clients, _client_loop, _closing,
# _pending_closes, _failed_closes, _close_completed). Multiple event loops can
# live on different OS threads (tests and reload/shutdown paths exercise
# this), so compound read-modify-write sequences on these dicts need a real
# lock. Reentrant because _collect_completed_closes re-enters _schedule_close
# when rehoming clients. Never held across an await.
_state_lock = threading.RLock()

_clients: dict[str, httpx.AsyncClient] = {}
_client_loop: asyncio.AbstractEventLoop | None = None
_closing = False


@dataclass
class _CloseSubmission:
    client: httpx.AsyncClient
    completion: concurrent.futures.Future[None]
    task: asyncio.Task[None] | None = None
    retired: bool = False
    settlement_lock: threading.Lock = field(default_factory=threading.Lock)
    settled_outcome: tuple[str, object | None] | None = None


_pending_closes: dict[
    asyncio.AbstractEventLoop,
    dict[concurrent.futures.Future[None], _CloseSubmission],
] = {}
_failed_closes: dict[asyncio.AbstractEventLoop, set[httpx.AsyncClient]] = {}
_close_completed: weakref.WeakKeyDictionary[httpx.AsyncClient, bool] = (
    weakref.WeakKeyDictionary()
)


class _StatelessCookies(httpx.Cookies):
    """Prevent response cookies from leaking between callers sharing a pool."""

    def extract_cookies(self, response: httpx.Response) -> None:
        return


def upstream_origin_key(url: str) -> str:
    """Return a canonical origin for an absolute HTTP(S) URL."""
    error = "Upstream URL must be an absolute HTTP(S) URL with a valid authority"
    if not isinstance(url, str):
        raise ValueError(error)
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError(error) from exc

    scheme = parts.scheme.lower()
    authority = parts.netloc.rsplit("@", 1)[-1]
    if (
        scheme not in {"http", "https"}
        or not hostname
        or "@" in parts.netloc
        or any(character.isspace() for character in hostname)
        or authority.endswith(":")
    ):
        raise ValueError(error)

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        # HTTPX URL serialization applies the same IDNA normalization used for
        # requests, so Unicode and punycode spellings share one pool key.
        normalized = httpx.URL(url).copy_with(
            username=None,
            password=None,
            path="/",
            query=None,
            fragment=None,
        )
        return str(normalized).rstrip("/")

    canonical_host = address.compressed
    if address.version == 6:
        canonical_host = f"[{canonical_host}]"
    default_port = 80 if scheme == "http" else 443
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{canonical_host}{port_suffix}"


def _build_client() -> httpx.AsyncClient:
    limits = httpx.Limits(
        max_connections=settings.upstream_max_connections,
        max_keepalive_connections=settings.upstream_max_keepalive_connections,
        keepalive_expiry=settings.upstream_keepalive_expiry,
    )
    client = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(
            limits=limits,
            retries=settings.upstream_connect_retries,
        ),
        timeout=httpx.Timeout(
            connect=settings.upstream_connect_timeout,
            read=settings.upstream_read_timeout,
            write=settings.upstream_write_timeout,
            pool=settings.upstream_pool_timeout,
        ),
    )
    # AsyncClient's public setter copies into a concrete Cookies jar, so replace
    # the backing jar directly to keep response cookies out of it.
    client._cookies = _StatelessCookies()
    return client


def _close_is_pending(client: httpx.AsyncClient) -> bool:
    with _state_lock:
        return any(
            submission.client is client
            for closes in _pending_closes.values()
            for submission in closes.values()
        )


def _forget_failed_client(client: httpx.AsyncClient) -> None:
    with _state_lock:
        for failed_loop, failed in list(_failed_closes.items()):
            failed.discard(client)
            if not failed:
                _failed_closes.pop(failed_loop, None)


async def _close_client_resources(client: httpx.AsyncClient) -> None:
    if not client.is_closed:
        await client.aclose()
        return

    # HTTPX marks the client closed before awaiting its transports. A retry after
    # cancellation or failure therefore has to resume at the transport boundary.
    raw_client = cast(Any, client)
    resources = [raw_client._transport]
    resources.extend(
        proxy for proxy in raw_client._mounts.values() if proxy is not None
    )
    seen: set[int] = set()
    for resource in resources:
        if id(resource) in seen:
            continue
        seen.add(id(resource))
        await resource.aclose()


def _close_task_outcome(
    completed: asyncio.Task[None],
) -> tuple[str, object | None]:
    if completed.cancelled():
        return ("cancelled", None)
    exception = completed.exception()
    if exception is not None:
        return ("exception", exception)
    return ("result", completed.result())


def _matching_close_outcomes(
    first: tuple[str, object | None], second: tuple[str, object | None]
) -> bool:
    if first[0] != second[0]:
        return False
    if first[0] == "cancelled":
        return True
    return first[1] is second[1]


def _settle_close_submission(
    submission: _CloseSubmission, completed: asyncio.Task[None]
) -> None:
    outcome = _close_task_outcome(completed)
    with submission.settlement_lock:
        if submission.settled_outcome is not None:
            if _matching_close_outcomes(submission.settled_outcome, outcome):
                return
            raise RuntimeError("Close submission settled with conflicting outcomes")
        if submission.completion.done():
            raise RuntimeError("Close submission completion changed before settlement")

        if outcome[0] == "result":
            submission.completion.set_result(None)
        elif outcome[0] == "exception":
            submission.completion.set_exception(cast(BaseException, outcome[1]))
        else:
            submission.completion.set_exception(asyncio.CancelledError())
        submission.settled_outcome = outcome


def _settle_submission_from_task(submission: _CloseSubmission) -> None:
    task = submission.task
    if task is not None and task.done():
        _settle_close_submission(submission, task)


def _finish_close_submission(
    submission: _CloseSubmission, completed: asyncio.Task[None]
) -> None:
    if not submission.retired:
        _settle_close_submission(submission, completed)


def _submit_close(
    client: httpx.AsyncClient, loop: asyncio.AbstractEventLoop
) -> _CloseSubmission:
    """Submit a close without creating its coroutine until the loop runs it."""
    submission = _CloseSubmission(client, concurrent.futures.Future())

    def start() -> None:
        if not submission.completion.set_running_or_notify_cancel():
            return
        submission.task = loop.create_task(_close_client_resources(client))

        submission.task.add_done_callback(
            lambda completed: _finish_close_submission(submission, completed)
        )

    loop.call_soon_threadsafe(start)
    return submission


def _collect_completed_closes() -> None:
    current_loop = asyncio.get_running_loop()
    rehome: list[httpx.AsyncClient] = []
    with _state_lock:
        for loop, closes in list(_pending_closes.items()):
            for future, submission in list(closes.items()):
                client = submission.client
                _settle_submission_from_task(submission)
                if not future.done():
                    if submission.task is None and not loop.is_running():
                        submission.retired = True
                        future.cancel()
                        closes.pop(future)
                        rehome.append(client)
                    elif loop.is_closed():
                        # A task on a closed loop cannot resume, so it cannot
                        # race a retry at the owned transport boundary.
                        submission.retired = True
                        closes.pop(future)
                        rehome.append(client)
                    continue
                closes.pop(future)
                try:
                    future.result()
                except concurrent.futures.CancelledError:
                    rehome.append(client)
                except asyncio.CancelledError:
                    _close_completed.pop(client, None)
                    _failed_closes.setdefault(loop, set()).add(client)
                except Exception as exc:
                    _close_completed.pop(client, None)
                    _failed_closes.setdefault(loop, set()).add(client)
                    logger.warning(
                        "Failed to close upstream HTTP client",
                        extra={"error": str(exc), "error_type": type(exc).__name__},
                    )
                else:
                    _close_completed[client] = True
                    _forget_failed_client(client)
            if not closes:
                _pending_closes.pop(loop, None)

        for client in rehome:
            _schedule_close(client, current_loop)


def _resume_stopped_loop(
    loop: asyncio.AbstractEventLoop,
    tasks: list[asyncio.Task[None]],
    timeout: float,
) -> bool:
    if loop.is_closed() or loop.is_running():
        return False

    async def wait_for_tasks() -> None:
        await asyncio.wait(tasks, timeout=timeout)

    waiter = wait_for_tasks()
    try:
        loop.run_until_complete(waiter)
    except RuntimeError:
        waiter.close()
        return False
    return all(task.done() for task in tasks)


async def _drain_pending_closes(timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        _collect_completed_closes()
        with _state_lock:
            if not _pending_closes:
                return
            if all(loop.is_closed() for loop in _pending_closes):
                return
            pending_snapshot = [
                (
                    owner_loop,
                    [
                        submission.task
                        for submission in closes.values()
                        if submission.task is not None and not submission.task.done()
                    ],
                )
                for owner_loop, closes in _pending_closes.items()
            ]

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            logger.error(
                "Timed out draining upstream HTTP client closes; retaining them for retry"
            )
            return

        resumed = False
        for owner_loop, tasks in pending_snapshot:
            if owner_loop.is_closed() or owner_loop.is_running():
                continue
            if not tasks:
                continue
            resumed = True
            await asyncio.to_thread(
                _resume_stopped_loop,
                owner_loop,
                tasks,
                remaining,
            )
            _collect_completed_closes()

        if not resumed:
            await asyncio.sleep(min(0.01, remaining))


def _schedule_close(
    client: httpx.AsyncClient, owner_loop: asyncio.AbstractEventLoop
) -> None:
    """Schedule closure on the owning loop, retaining unfinished work."""
    with _state_lock:
        if _close_completed.get(client, False):
            _forget_failed_client(client)
            return
        if _close_is_pending(client):
            return

        current_loop = asyncio.get_running_loop()
        execution_loop = owner_loop
        if owner_loop.is_closed() or not owner_loop.is_running():
            execution_loop = current_loop
            logger.warning(
                "Closing upstream HTTP client outside its inactive event loop"
            )

        try:
            submission = _submit_close(client, execution_loop)
        except RuntimeError:
            if execution_loop is current_loop:
                _failed_closes.setdefault(owner_loop, set()).add(client)
                return
            logger.warning("Upstream HTTP client event loop stopped during shutdown")
            submission = _submit_close(client, current_loop)
            execution_loop = current_loop

        _forget_failed_client(client)
        _pending_closes.setdefault(execution_loop, {})[submission.completion] = (
            submission
        )


def get_upstream_http_client(url: str) -> httpx.AsyncClient:
    """Return the shared client for an absolute upstream URL's origin."""
    global _client_loop
    loop = asyncio.get_running_loop()
    with _state_lock:
        if _closing:
            raise RuntimeError("Upstream HTTP client is shutting down")

        _collect_completed_closes()
        if _client_loop is not loop:
            stale_clients = list(_clients.values())
            stale_loop = _client_loop
            _clients.clear()
            _client_loop = loop
            if stale_loop is not None:
                for stale_client in stale_clients:
                    _schedule_close(stale_client, stale_loop)

        key = upstream_origin_key(url)
        client = _clients.get(key)
        if client is not None and not client.is_closed:
            return client
        client = _build_client()
        _clients[key] = client
        logger.debug(
            "Opened upstream HTTP connection pool",
            extra={
                "origin": key,
                "max_connections": settings.upstream_max_connections,
                "max_keepalive_connections": settings.upstream_max_keepalive_connections,
                "pool_timeout": settings.upstream_pool_timeout,
                "read_timeout": settings.upstream_read_timeout,
            },
        )
    return client


async def close_upstream_http_client() -> None:
    """Close every pool, using its owner loop while that loop remains active."""
    global _client_loop, _closing

    with _state_lock:
        _collect_completed_closes()
        clients = list(_clients.values())
        owner_loop = _client_loop
        failed_clients = [
            (failed_loop, client)
            for failed_loop, failed in _failed_closes.items()
            for client in failed
        ]
        if not clients and not failed_clients and not _pending_closes:
            return

        _closing = True
        _clients.clear()
        _client_loop = None
        if owner_loop is not None:
            for client in clients:
                _schedule_close(client, owner_loop)
        for failed_loop, client in failed_clients:
            _schedule_close(client, failed_loop)

    try:
        await _drain_pending_closes()
    finally:
        with _state_lock:
            _closing = False
