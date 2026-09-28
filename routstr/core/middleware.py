import time
import uuid
from contextvars import ContextVar
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware

from .logging import get_logger
from .settings import settings

logger = get_logger(__name__)

# Context variable to store request ID across async context
request_id_context: ContextVar[str | None] = ContextVar("request_id")

client_app_context: ContextVar[str | None] = ContextVar("client_app")

UNKNOWN_CLIENT_APP = "unknown"

# Prefer OpenRouter app headers, then browser and SDK fallbacks.
_CLIENT_APP_HEADERS: tuple[str, ...] = (
    "x-title",
    "http-referer",
    "referer",
    "user-agent",
)

# Limit untrusted header data repeated in every log record.
_CLIENT_APP_MAX_LENGTH = 120


def client_app_from_headers(headers: Headers) -> str:
    for header in _CLIENT_APP_HEADERS:
        raw = headers.get(header)
        if raw is None:
            continue
        cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()
        if header in ("http-referer", "referer"):
            try:
                url = urlsplit(cleaned)
                if url.scheme not in ("http", "https") or not url.hostname:
                    continue
            except ValueError:
                continue
            # Attribution needs the origin, not credentials or private page URLs.
            cleaned = f"{url.scheme}://{url.netloc.rsplit('@', 1)[-1]}"
        if cleaned:
            return cleaned[:_CLIENT_APP_MAX_LENGTH]
    return UNKNOWN_CLIENT_APP


# Methods that are never logged: HEAD requests are health probes from
# monitoring/load balancers, OPTIONS are CORS preflights — both are framework
# chatter, not user-meaningful events.
_SKIP_LOG_METHODS: frozenset[str] = frozenset({"HEAD", "OPTIONS"})

# Path prefixes to skip. Includes Next.js static chunks and the admin
# dashboard's internal polling API (/admin/api/*) which the UI hits on a timer
# to refresh balances, logs, providers, etc. — high volume, low diagnostic
# value. Mutating admin actions are recorded separately in the audit log.
_SKIP_LOG_PREFIXES: tuple[str, ...] = (
    "/_next/",
    "/admin/api/",
)

# Exact paths to skip. RSC payload prefetches (`*/index.txt`) fire automatically
# as the user hovers near `<Link>`s, and `/v1/wallet/info` is polled by the UI.
_SKIP_LOG_EXACT: frozenset[str] = frozenset(
    {
        "/favicon.ico",
        "/icon.ico",
        "/v1/wallet/info",
        "/index.txt",
        "/login/index.txt",
        "/model/index.txt",
        "/providers/index.txt",
        "/settings/index.txt",
        "/transactions/index.txt",
        "/balances/index.txt",
        "/logs/index.txt",
        "/usage/index.txt",
        "/unauthorized/index.txt",
    }
)


def _should_log(method: str, path: str, status_code: int | None = None) -> bool:
    if method in _SKIP_LOG_METHODS:
        return False
    # Our own faults are never noise, whatever the path.
    if status_code is not None and status_code >= 500:
        return True
    if path in _SKIP_LOG_EXACT:
        # A 4xx storm on a UI-polled path is exactly what we need to see.
        return status_code is not None and status_code >= 400
    # Client errors on the skipped prefixes stay hidden: 404s under /_next/ are
    # driven by whoever scans the node, and the admin UI's timer-driven polling
    # turns one expired session into a 401 per poll.
    return not any(path.startswith(prefix) for prefix in _SKIP_LOG_PREFIXES)


def _attribution(request: Request) -> dict[str, object]:
    """Model/provider fields, omitted rather than null on routes that resolve none."""
    return {
        field: value
        for field in ("model", "provider")
        if (value := getattr(request.state, field, None))
    }


def mark(request: Request, name: str) -> None:
    """Record that stage ``name`` finished, for the completion log's timings."""
    marks = getattr(request.state, "stage_marks", None)
    if marks is not None:
        marks[name] = time.monotonic()


def _request_content_length(headers: Headers) -> int | None:
    """Client-supplied length, dropped unless it is a plausible byte count."""
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log proxy interactions and page navigation.

    Skips logging for static assets and Next.js chunks to avoid noise.
    """

    def _log_completion(
        self,
        *,
        request: Request,
        request_id: str,
        path: str,
        status_code: int,
        duration: float,
        headers_duration: float | None,
        stage_start: float,
        stage_marks: dict[str, float],
        incoming_logged: bool,
    ) -> None:
        if not _should_log(request.method, path, status_code):
            return

        extra: dict[str, object] = {
            "request_id": request_id,
            "method": request.method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round(duration * 1000, 2),
            "content_length": _request_content_length(request.headers),
            **_attribution(request),
        }
        if headers_duration is not None:
            extra["time_to_headers_ms"] = round(headers_duration * 1000, 2)
        if not incoming_logged:
            # Tells log consumers that join on request_id why the matching
            # "Incoming request" record is missing.
            extra["incoming_suppressed"] = True
        for name, marked_at in stage_marks.items():
            extra[f"{name}_ms"] = round((marked_at - stage_start) * 1000, 2)
        if status_code >= 400:
            error_detail = getattr(request.state, "error_detail", None)
            if isinstance(error_detail, dict):
                extra["error_type"] = error_detail.get("error_type")
                extra["error_code"] = error_detail.get("error_code")
                extra["error_message"] = error_detail.get("error_message")
        log = (
            logger.warning
            if duration > settings.slow_request_warn_seconds
            else logger.info
        )
        log("Request completed", extra=extra)

    async def _timed_body(
        self,
        body_iterator: AsyncIterator[bytes],
        *,
        request: Request,
        request_id: str,
        client_app: str,
        path: str,
        status_code: int,
        stage_start: float,
        stage_marks: dict[str, float],
        headers_duration: float,
        incoming_logged: bool,
    ) -> AsyncIterator[bytes]:
        try:
            async for chunk in body_iterator:
                yield chunk
        finally:
            duration = time.monotonic() - stage_start
            # dispatch() has already reset both context vars by now, and the
            # logging filters read request_id/client_app from them.
            request_token = request_id_context.set(request_id)
            app_token = client_app_context.set(client_app)
            try:
                self._log_completion(
                    request=request,
                    request_id=request_id,
                    path=path,
                    status_code=status_code,
                    duration=duration,
                    headers_duration=headers_duration,
                    stage_start=stage_start,
                    stage_marks=stage_marks,
                    incoming_logged=incoming_logged,
                )
            finally:
                request_id_context.reset(request_token)
                client_app_context.reset(app_token)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Generate request ID
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        # Set request ID in context for logging
        token = request_id_context.set(request_id)

        client_app = client_app_from_headers(request.headers)
        client_app_token = client_app_context.set(client_app)

        path = request.url.path
        should_log = _should_log(request.method, path)

        # Start timing. Monotonic throughout: a wall-clock step would otherwise
        # produce negative durations and bogus slow-request warnings.
        stage_start = time.monotonic()
        stage_marks: dict[str, float] = {}
        request.state.stage_marks = stage_marks

        if should_log:
            logger.info(
                "Incoming request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    # Names only: query values carry API keys and refund
                    # tokens on the wallet routes.
                    "query_param_names": sorted(request.query_params.keys()),
                },
            )

        # Process request
        try:
            response = await call_next(request)

            headers_duration = time.monotonic() - stage_start

            if hasattr(response, "headers"):
                response.headers["x-routstr-request-id"] = request_id
                # Headers are already on the wire before a streamed body ends,
                # so this can only ever be time-to-headers.
                response.headers["x-routstr-duration-ms"] = str(
                    round(headers_duration * 1000, 2)
                )

            body_iterator = getattr(response, "body_iterator", None)
            if body_iterator is None:
                self._log_completion(
                    request=request,
                    request_id=request_id,
                    path=path,
                    status_code=response.status_code,
                    duration=headers_duration,
                    headers_duration=None,
                    stage_start=stage_start,
                    stage_marks=stage_marks,
                    incoming_logged=should_log,
                )
                return response

            # A StreamingResponse is barely started here: most of the time a
            # slow completion spends in the node is spent relaying its body, so
            # the completion log has to wait for the iterator to drain.
            response.body_iterator = self._timed_body(
                body_iterator,
                request=request,
                request_id=request_id,
                client_app=client_app,
                path=path,
                status_code=response.status_code,
                stage_start=stage_start,
                stage_marks=stage_marks,
                headers_duration=headers_duration,
                incoming_logged=should_log,
            )

            return response

        except Exception as e:
            # Always log failures, even for skipped paths, so we don't lose errors.
            duration = time.monotonic() - stage_start
            logger.error(
                "Request failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    "duration_ms": round(duration * 1000, 2),
                    "error": str(e),
                    "error_type": type(e).__name__,
                    **_attribution(request),
                },
                exc_info=True,
            )
            raise
        finally:
            # Reset context
            request_id_context.reset(token)
            client_app_context.reset(client_app_token)


__all__ = [
    "LoggingMiddleware",
    "UNKNOWN_CLIENT_APP",
    "client_app_context",
    "mark",
    "request_id_context",
]
