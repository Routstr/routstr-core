from fastapi import Request
from fastapi.responses import JSONResponse

from .logging import get_logger

logger = get_logger(__name__)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle HTTP exceptions and include request ID in response."""
    request_id = getattr(request.state, "request_id", "unknown")

    # Get status code and detail - works for both FastAPI and Starlette HTTPException
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", str(exc))

    logger.warning(
        "HTTP exception",
        extra={
            "request_id": request_id,
            "status_code": status_code,
            "detail": detail,
            "path": request.url.path,
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
