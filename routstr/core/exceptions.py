from fastapi import Request
from fastapi.responses import JSONResponse

from .logging import get_logger

logger = get_logger(__name__)


class UpstreamError(Exception):
    """Exception raised when an upstream provider fails.

    ``code`` carries a stable, machine-readable classification (e.g.
    ``UPSTREAM_RATE_LIMIT``) so callers can distinguish failure kinds without
    string-matching the message. ``details`` holds optional structured,
    redaction-safe context. Both default to ``None`` for backwards
    compatibility.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 502,
        code: str | None = None,
        details: dict[str, object] | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details
        super().__init__(message)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle HTTP exceptions and include request ID in response."""
    request_id = getattr(request.state, "request_id", "unknown")

    # Get status code and detail - works for both FastAPI and Starlette HTTPException
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", str(exc))
    path = request.url.path

    # 4xx is client behaviour; the uvicorn access log already records it.
    # Only 5xx warrants a server-side warning/error log here.
    if status_code >= 500:
        logger.error(
            f"HTTP {status_code} on {path}: {detail}",
            extra={
                "request_id": request_id,
                "status_code": status_code,
                "detail": detail,
                "path": path,
            },
        )

    return JSONResponse(
        status_code=status_code,
        content={
            "detail": detail,
            "request_id": request_id,
        },
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle general exceptions and include request ID in response."""
    request_id = getattr(request.state, "request_id", "unknown")

    logger.error(
        "Unhandled exception",
        extra={
            "request_id": request_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "path": request.url.path,
        },
        exc_info=True,
    )

    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error, please contact support with the request ID.",
            "request_id": request_id,
        },
    )
