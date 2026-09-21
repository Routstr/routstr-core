"""Regression tests for the 4xx "Request completed" log.

The middleware's completion log used to contain only the status code; the
exception handler gated its log on ``status_code >= 500`` and never stashed
anything on ``request.state``.  A 400 returned from
``http_exception_handler`` therefore produced a bare ``Request completed``
with no ``error_type``, ``error_code``, or ``error_message`` — the reason
was lost.

These tests assert the new behaviour at two levels:

- ``http_exception_handler`` writes a structured INFO record for 4xx and
  stashes a safe, truncated ``error_detail`` dict on ``request.state``.
- ``LoggingMiddleware`` picks that stash up and adds the three fields to
  the completion log for any ``status_code >= 400``.
"""

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pythonjsonlogger import jsonlogger
from starlette.requests import Request

from routstr.core.exceptions import http_exception_handler
from routstr.core.logging import (
    DailyRotatingFileHandler,
    RequestIdFilter,
    SecurityFilter,
    VersionFilter,
)
from routstr.core.middleware import LoggingMiddleware


@pytest.fixture
def handler(tmp_path: Path) -> Iterator[DailyRotatingFileHandler]:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    h = DailyRotatingFileHandler(
        str(log_dir / "app.log"), when="midnight", interval=1, backupCount=30
    )
    h.setLevel(logging.DEBUG)
    h.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s %(pathname)s "
            "%(lineno)d %(version)s %(request_id)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    for f in (VersionFilter(), RequestIdFilter(), SecurityFilter()):
        h.addFilter(f)
    try:
        yield h
    finally:
        h.close()


def _read_last_record(handler: DailyRotatingFileHandler) -> dict[str, Any]:
    handler.flush()
    text = Path(handler.baseFilename).read_text()
    assert text.strip(), "log file is empty"
    import json as _json

    return _json.loads(text.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# http_exception_handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_exception_handler_logs_400_at_info() -> None:
    """4xx used to skip logging entirely; now it must log at INFO."""
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/wallet/refund",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )
    request.state.request_id = "req-4xx"

    calls: list[tuple[tuple, dict]] = []
    with pytest.MonkeyPatch.context() as m:
        m.setattr(
            "routstr.core.exceptions.logger.info",
            lambda *a, **k: calls.append((a, k)),
        )
        await http_exception_handler(
            request,
            HTTPException(status_code=400, detail="Balance too small to refund"),
        )

    assert calls, "logger.info was not called for a 4xx response"
    extra = calls[0][1].get("extra", {})
    assert extra["status_code"] == 400
    assert extra["request_id"] == "req-4xx"
    assert extra["path"] == "/v1/wallet/refund"


@pytest.mark.asyncio
async def test_http_exception_handler_stashes_error_detail_for_middleware() -> None:
    """The handler must leave a safe copy on ``request.state.error_detail``."""
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )
    request.state.request_id = "req-abc"

    await http_exception_handler(
        request,
        HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "Invalid token format",
                    "type": "invalid_request_error",
                    "code": "invalid_token",
                }
            },
        ),
    )

    assert request.state.error_detail == {
        "error_type": "invalid_request_error",
        "error_code": "invalid_token",
        "error_message": "Invalid token format",
    }


@pytest.mark.asyncio
async def test_http_exception_handler_stash_truncates_long_string_details() -> None:
    """The stashed message must be capped, so a giant exception body does
    not blow up the middleware log line."""
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )
    request.state.request_id = "req-trunc"

    long_detail = "x" * 500
    await http_exception_handler(
        request, HTTPException(status_code=400, detail=long_detail)
    )

    msg = request.state.error_detail["error_message"]
    assert isinstance(msg, str)
    assert len(msg) == 200  # truncation cap


# ---------------------------------------------------------------------------
# LoggingMiddleware integration
# ---------------------------------------------------------------------------


def _build_app_with_middleware(handler: DailyRotatingFileHandler) -> FastAPI:
    """Build a minimal app wired like production (middleware + custom handler).

    FastAPI's default ``HTTPException`` handler would catch the raise before
    ``http_exception_handler`` runs and would not populate ``request.state``.
    Registering the custom handler mirrors ``main.py``.
    """
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore

    @app.get("/boom")
    async def boom() -> None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "Balance too small to refund",
                    "type": "invalid_request_error",
                    "code": "balance_too_small",
                }
            },
        )

    return app


def test_middleware_completion_log_includes_error_detail_for_4xx(
    handler: DailyRotatingFileHandler,
) -> None:
    """The completion log for a 4xx must carry error_type/code/message.

    Uses the same handler + logger wiring pattern as
    ``test_middleware_logs_query_param_names_without_values``.
    """
    app = _build_app_with_middleware(handler)

    middleware_logger = logging.getLogger("routstr.core.middleware")
    middleware_logger.setLevel(logging.INFO)
    middleware_logger.propagate = False
    original_handlers = middleware_logger.handlers
    middleware_logger.handlers = [handler]
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/boom")
        assert response.status_code == 400
    finally:
        middleware_logger.handlers = original_handlers

    record = _read_last_record(handler)
    assert record["message"] == "Request completed"
    assert record["status_code"] == 400
    assert record["error_type"] == "invalid_request_error"
    assert record["error_code"] == "balance_too_small"
    assert record["error_message"] == "Balance too small to refund"


def test_middleware_completion_log_omits_error_fields_for_2xx(
    handler: DailyRotatingFileHandler,
) -> None:
    """2xx responses must not gain the error fields."""
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    middleware_logger = logging.getLogger("routstr.core.middleware")
    middleware_logger.setLevel(logging.INFO)
    middleware_logger.propagate = False
    original_handlers = middleware_logger.handlers
    middleware_logger.handlers = [handler]
    try:
        with TestClient(app) as client:
            response = client.get("/ok")
        assert response.status_code == 200
    finally:
        middleware_logger.handlers = original_handlers

    record = _read_last_record(handler)
    assert record["message"] == "Request completed"
    assert record["status_code"] == 200
    assert "error_type" not in record
    assert "error_code" not in record
    assert "error_message" not in record
