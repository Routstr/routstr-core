"""Tests for client-app identification in request logging."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from routstr.core.logging import ClientAppFilter
from routstr.core.middleware import (
    UNKNOWN_CLIENT_APP,
    LoggingMiddleware,
    client_app_context,
    client_app_from_headers,
)


def _record() -> logging.LogRecord:
    return logging.LogRecord(
        name="routstr.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test",
        args=None,
        exc_info=None,
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (
            {
                "x-title": "Goose",
                "http-referer": "https://myapp.example.com",
                "user-agent": "python-httpx/0.27",
            },
            "Goose",
        ),
        (
            {"http-referer": "https://myapp.example.com", "user-agent": "curl/8.4.0"},
            "https://myapp.example.com",
        ),
        (
            {"referer": "https://myapp.example.com", "user-agent": "curl/8.4.0"},
            "https://myapp.example.com",
        ),
        ({"user-agent": "curl/8.4.0"}, "curl/8.4.0"),
        ({}, UNKNOWN_CLIENT_APP),
        ({"x-title": "   ", "user-agent": "curl/8.4.0"}, "curl/8.4.0"),
        ({"x-title": "   ", "user-agent": "\t"}, UNKNOWN_CLIENT_APP),
    ],
    ids=[
        "x-title-wins",
        "http-referer",
        "referer",
        "user-agent-fallback",
        "no-identity-headers",
        "blank-falls-through",
        "all-blank",
    ],
)
def test_client_app_from_headers(headers: dict[str, str], expected: str) -> None:
    assert client_app_from_headers(Headers(headers)) == expected


def test_value_is_truncated_to_120_chars() -> None:
    assert client_app_from_headers(Headers({"x-title": "a" * 500})) == "a" * 120


def test_control_characters_are_stripped() -> None:
    """A crafted header must not be able to forge log records."""
    headers = Headers({"user-agent": "evil-app\x1b[0m fake INFO line"})
    assert client_app_from_headers(headers) == "evil-app[0m fake INFO line"


def test_filter_reads_context_variable() -> None:
    token = client_app_context.set("Goose")
    try:
        record = _record()
        assert ClientAppFilter().filter(record) is True
        assert record.client_app == "Goose"  # type: ignore[attr-defined]
    finally:
        client_app_context.reset(token)


def test_filter_defaults_to_unknown_outside_request_context() -> None:
    record = _record()
    assert ClientAppFilter().filter(record) is True
    assert record.client_app == UNKNOWN_CLIENT_APP  # type: ignore[attr-defined]


def test_handler_logs_carry_client_app(caplog: pytest.LogCaptureFixture) -> None:
    """A log line emitted inside a handler still names the app that triggered it."""
    app = FastAPI()
    handler_logger = logging.getLogger("routstr.test.handler")

    @app.get("/whoami")
    async def whoami() -> dict[str, bool]:
        handler_logger.warning("something went wrong")
        return {"ok": True}

    app.add_middleware(LoggingMiddleware)

    caplog.handler.addFilter(ClientAppFilter())
    handler_logger.addHandler(caplog.handler)
    try:
        TestClient(app).get("/whoami", headers={"X-Title": "Goose"})
    finally:
        handler_logger.removeHandler(caplog.handler)

    record = next(r for r in caplog.records if r.name == "routstr.test.handler")
    assert record.client_app == "Goose"  # type: ignore[attr-defined]
