"""Tests for error detail in the 4xx "Request completed" log."""

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
    assert len(msg) == 200


# ---------------------------------------------------------------------------
# LoggingMiddleware integration
# ---------------------------------------------------------------------------


def _build_app_with_middleware(handler: DailyRotatingFileHandler) -> FastAPI:
    """Middleware + custom handler, wired like ``main.py``."""
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
