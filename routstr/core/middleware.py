import time
import uuid
from contextvars import ContextVar
from typing import Callable

from fastapi import Request, Response
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware

from .logging import get_logger

logger = get_logger(__name__)

# Context variable to store request ID across async context
request_id_context: ContextVar[str | None] = ContextVar("request_id")

# Context variable to store the client app across async context
client_app_context: ContextVar[str | None] = ContextVar("client_app")

UNKNOWN_CLIENT_APP = "unknown"

# Identity headers in priority order. X-Title and HTTP-Referer are the
# OpenRouter convention; User-Agent covers SDKs and scripts that set neither.
_CLIENT_APP_HEADERS: tuple[str, ...] = (
    "x-title",
    "http-referer",
    "referer",
    "user-agent",
)

# Header values are attacker-controlled: cap the length so one request can't
# bloat every log line.
_CLIENT_APP_MAX_LENGTH = 120


def client_app_from_headers(headers: Headers) -> str:
    for header in _CLIENT_APP_HEADERS:
        raw = headers.get(header)
        if raw is None:
            continue
        cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()
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


def _should_log(method: str, path: str) -> bool:
    if method in _SKIP_LOG_METHODS:
        return False
    if path in _SKIP_LOG_EXACT:
        return False
    return not any(path.startswith(prefix) for prefix in _SKIP_LOG_PREFIXES)


class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log proxy interactions and page navigation.

    Skips logging for static assets and Next.js chunks to avoid noise.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Generate request ID
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        # Set request ID in context for logging
        token = request_id_context.set(request_id)

        client_app_token = client_app_context.set(
            client_app_from_headers(request.headers)
        )

        path = request.url.path
        should_log = _should_log(request.method, path)

        # Start timing
        start_time = time.time()

        if should_log:
            logger.info(
                "Incoming request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    "query_params": dict(request.query_params),
                },
            )

        # Process request
        try:
            response = await call_next(request)

            if should_log:
                duration = time.time() - start_time
                logger.info(
                    "Request completed",
                    extra={
                        "request_id": request_id,
                        "method": request.method,
                        "path": path,
                        "status_code": response.status_code,
                        "duration_ms": round(duration * 1000, 2),
                    },
                )
            if hasattr(response, "headers"):
                response.headers["x-routstr-request-id"] = request_id

            return response

        except Exception as e:
            # Always log failures, even for skipped paths, so we don't lose errors.
            duration = time.time() - start_time
            logger.error(
                "Request failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    "duration_ms": round(duration * 1000, 2),
                    "error": str(e),
                    "error_type": type(e).__name__,
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
    "request_id_context",
]
