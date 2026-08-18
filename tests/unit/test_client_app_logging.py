"""Tests for client-app identification in request logging."""

import logging

from fastapi import FastAPI, Request
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


# ---------------------------------------------------------------------------
# client_app_from_headers
# ---------------------------------------------------------------------------


def test_x_title_takes_priority() -> None:
    """X-Title wins over Referer and User-Agent."""
    headers = Headers(
        {
            "x-title": "Goose",
            "referer": "https://myapp.example.com",
            "user-agent": "python-httpx/0.27",
        }
    )
    assert client_app_from_headers(headers) == "Goose"


def test_referer_used_when_no_x_title() -> None:
    headers = Headers(
        {"referer": "https://myapp.example.com", "user-agent": "python-httpx/0.27"}
    )
    assert client_app_from_headers(headers) == "https://myapp.example.com"


def test_user_agent_is_last_fallback() -> None:
    assert (
        client_app_from_headers(Headers({"user-agent": "curl/8.4.0"})) == "curl/8.4.0"
    )


def test_unknown_when_no_identity_headers() -> None:
    assert client_app_from_headers(Headers({})) == UNKNOWN_CLIENT_APP


def test_blank_header_falls_through_to_next() -> None:
    """A whitespace-only X-Title must not shadow a usable User-Agent."""
    headers = Headers({"x-title": "   ", "user-agent": "curl/8.4.0"})
    assert client_app_from_headers(headers) == "curl/8.4.0"


def test_all_blank_resolves_to_unknown() -> None:
    headers = Headers({"x-title": "   ", "user-agent": "\t"})
    assert client_app_from_headers(headers) == UNKNOWN_CLIENT_APP


def test_value_is_truncated_to_120_chars() -> None:
    assert client_app_from_headers(Headers({"x-title": "a" * 500})) == "a" * 120


def test_control_characters_are_stripped() -> None:
    """A crafted header must not be able to forge log records."""
    headers = Headers({"user-agent": "evil-app\x1b[0m fake INFO line"})
    assert client_app_from_headers(headers) == "evil-app[0m fake INFO line"


# ---------------------------------------------------------------------------
# ClientAppFilter
# ---------------------------------------------------------------------------


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


def test_filter_keeps_explicit_extra() -> None:
    """extra={"client_app": ...} on a log call wins over the context value."""
    token = client_app_context.set("context-app")
    try:
        record = _record()
        record.client_app = "explicit-app"  # type: ignore[attr-defined]
        assert ClientAppFilter().filter(record) is True
        assert record.client_app == "explicit-app"  # type: ignore[attr-defined]
    finally:
        client_app_context.reset(token)


# ---------------------------------------------------------------------------
# LoggingMiddleware integration
# ---------------------------------------------------------------------------


def test_middleware_exposes_client_app_on_request_state() -> None:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request) -> dict:
        return {"client_app": request.state.client_app}

    app.add_middleware(LoggingMiddleware)
    client = TestClient(app)

    response = client.get("/whoami", headers={"X-Title": "Goose"})
    assert response.json() == {"client_app": "Goose"}


def test_middleware_reports_unknown_without_identity_headers() -> None:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request) -> dict:
        return {"client_app": request.state.client_app}

    app.add_middleware(LoggingMiddleware)
    # TestClient sets its own User-Agent; blank it out to simulate a bare client.
    client = TestClient(app, headers={"user-agent": ""})

    response = client.get("/whoami")
    assert response.json() == {"client_app": UNKNOWN_CLIENT_APP}
