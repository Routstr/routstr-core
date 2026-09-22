import math

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .error_scope import ERROR_SCOPE_UPSTREAM, UPSTREAM_ERROR_STATUS
from .logging import get_logger

logger = get_logger(__name__)


class UpstreamError(Exception):
    """Exception raised when an upstream provider fails.

    ``code`` carries a stable, machine-readable classification (e.g.
    ``UPSTREAM_RATE_LIMIT``) so callers can distinguish failure kinds without
    string-matching the message. ``details`` holds optional structured,
    redaction-safe context. Both default to ``None`` for backwards
    compatibility.

    ``scope`` records who the failure belongs to. ``"upstream"`` (the default,
    since this class describes a provider hop that failed) means the node
    stayed healthy and the failure is reported to the caller as a non-5xx
    ``424`` with ``error.code = UPSTREAM_UNAVAILABLE`` (see
    :mod:`routstr.core.error_scope`). ``"node"`` means a fault inside this
    process that merely surfaced while talking to a provider; those keep their
    status so a node failure is never disguised as an upstream one.

    ``status_code`` always stays the provider's own status (or the status the
    failure was classified with) — the caller-visible mapping happens once, at
    response construction.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 502,
        code: str | None = None,
        details: dict[str, object] | None = None,
        scope: str = ERROR_SCOPE_UPSTREAM,
    ):
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details
        self.scope = scope
        super().__init__(message)


class EhbpTimeoutError(UpstreamError):
    """Raised when an EHBP upstream times out waiting for a response.

    Distinct from a generic :class:`UpstreamError` so callers can map the
    failure to a stable ``UPSTREAM_TIMEOUT`` code instead of a misleading
    ``500`` internal server error. The status is the upstream-failure status
    (424, deliberately non-5xx): the timeout happened on the provider hop, and
    reporting it as 504 told callers this node was the broken one.

    ``details`` carries optional structured, redaction-safe context and is
    forwarded to the client by ``create_upstream_error_response``.
    """

    def __init__(self, message: str, details: dict[str, object] | None = None):
        super().__init__(
            message,
            status_code=UPSTREAM_ERROR_STATUS,
            code="UPSTREAM_TIMEOUT",
            details=details,
        )


def _error_message_from_detail(detail: object) -> str | None:
    """Extract a message from an HTTPException ``detail``, capped at 200 chars."""
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            msg = error.get("message")
            return str(msg)[:200] if isinstance(msg, str) else None
        if isinstance(error, str):
            return error[:200]
        return None
    if isinstance(detail, str):
        return detail[:200]
    return None


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle HTTP exceptions and include request ID in response."""
    request_id = getattr(request.state, "request_id", "unknown")

    # Get status code and detail - works for both FastAPI and Starlette HTTPException
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", str(exc))
    path = request.url.path

    error_type: str | None = None
    error_code: str | None = None
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            error_type = error.get("type")
            error_code = error.get("code")

    # 5xx logs as error/warning, 4xx at INFO.
    if status_code >= 500:
        log_fn = (
            logger.warning
            if error_type in {"mint_unreachable", "mint_rate_limited"}
            else logger.error
        )
    else:
        log_fn = logger.info
    log_fn(
        f"HTTP {status_code} on {path}: {detail}",
        extra={
            "request_id": request_id,
            "status_code": status_code,
            "detail": detail,
            "path": path,
            "error_type": error_type,
            "error_code": error_code,
            "level": "http" if status_code < 500 else "server",
        },
    )
    # Stash for LoggingMiddleware's completion log.
    request.state.error_detail = {
        "error_type": error_type,
        "error_code": error_code,
        "error_message": _error_message_from_detail(detail),
    }

    if isinstance(detail, dict) and "error" in detail:
        content = {"detail": detail, **detail}
    else:
        content = {"detail": detail}
    content["request_id"] = request_id

    headers = getattr(exc, "headers", None)
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def json_compliant(value: object) -> object:
    """Render non-finite floats as text so a reply carrying them can serialize.

    ``json`` parses the bare ``NaN``/``Infinity``/``-Infinity`` literals into
    real floats, so a request body — and a stored row written from one — may
    hold one anywhere. ``JSONResponse`` encodes with ``allow_nan=False`` and
    raises on them, which would turn a reply that merely *quotes* the offending
    value into a 500.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {key: json_compliant(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_compliant(item) for item in value]
    return value


async def validation_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Answer a request-validation failure with a 422 that always serializes.

    Pydantic echoes the rejected value back in each error's ``input`` field. A
    non-finite float there breaks the encoder, so the 422 escapes as a 500 and
    reports a client's bad rate as a server fault.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []

    return JSONResponse(
        status_code=422,
        content={
            "detail": json_compliant(jsonable_encoder(errors)),
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
