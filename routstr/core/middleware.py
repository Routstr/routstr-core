import time
import uuid
from contextvars import ContextVar
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .logging import get_logger

logger = get_logger(__name__)

# Context variable to store request ID across async context
request_id_context: ContextVar[str | None] = ContextVar("request_id")


class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log detailed request and response information."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Generate request ID
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        # Set request ID in context for logging
        token = request_id_context.set(request_id)

        # Start timing
        start_time = time.time()

        # Log request details
        request_body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            try:
                # Only read body for non-streaming requests
                if hasattr(request, "_body"):
                    request_body = await request.body()
            except Exception:
                pass

        # Extract request info
        client_host = None
        if request.client:
            client_host = request.client.host

        # Log incoming request
        logger.info(
            "Incoming request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "query_params": dict(request.query_params),
                "client_host": client_host,
                "headers": {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower()
                    not in [
                        "authorization",
                        "x-cashu",
                        "cookie",
                        "cf-connecting-ip",
                        "cf-ipcountry",
                        "x-forwarded-for",
                        "x-real-ip",
                    ]
                },
                "body_size": len(request_body) if request_body else 0,
            },
        )

        # Log at TRACE level for full body (security filter will redact sensitive data)
        if request_body and hasattr(logger, "exception"):
            logger.exception(
                "Request body",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "body": request_body.decode("utf-8", errors="ignore")[
                        :1000
                    ],  # Limit size
                },
            )

        # Process request
        try:
            response = await call_next(request)

            # Calculate duration
            duration = time.time() - start_time

            # Log response
            logger.info(
                "Request completed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration * 1000, 2),
                    "client_host": client_host,
                },
            )
            if hasattr(response, "headers"):
                response.headers["x-routstr-request-id"] = request_id

            return response

        except Exception as e:
            # Calculate duration
            duration = time.time() - start_time

            # Log error
            logger.error(
                "Request failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(duration * 1000, 2),
                    "client_host": client_host,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )
            raise
        finally:
            # Reset context
            request_id_context.reset(token)


__all__ = ["LoggingMiddleware", "request_id_context"]
