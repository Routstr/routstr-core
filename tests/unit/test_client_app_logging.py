"""Tests for client-app identification in request logging."""

import asyncio
import logging

import pytest
from fastapi import FastAPI, Request, Response
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


@pytest.mark.parametrize("header", ["http-referer", "referer"])
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://alice:password@app.example:8443/private/chat?token=secret#access_token=secret",
            "https://app.example:8443",
        ),
        ("http://[::1]:3000/chat?key=secret", "http://[::1]:3000"),
        ("https://app.example/" + "a" * 200, "https://app.example"),
        ("https://[invalid", "curl/8.4.0"),
        ("/private/chat?token=secret", "curl/8.4.0"),
        ("javascript:secret", "curl/8.4.0"),
        ("https:///private", "curl/8.4.0"),
    ],
)
def test_referrer_only_identifies_origin(header: str, url: str, expected: str) -> None:
    headers = Headers({header: url, "user-agent": "curl/8.4.0"})
    assert client_app_from_headers(headers) == expected


def test_value_is_truncated_to_120_chars() -> None:
    assert client_app_from_headers(Headers({"x-title": "a" * 500})) == "a" * 120


def test_control_characters_are_stripped() -> None:
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


@pytest.mark.parametrize("fail", [False, True])
async def test_context_is_restored_after_request(fail: bool) -> None:
    middleware = LoggingMiddleware(FastAPI())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "query_string": b"",
            "headers": [],
        }
    )

    async def call_next(request: Request) -> Response:
        assert client_app_context.get() == UNKNOWN_CLIENT_APP
        if fail:
            raise RuntimeError("handler failed")
        return Response()

    token = client_app_context.set("outer")
    try:
        if fail:
            with pytest.raises(RuntimeError, match="handler failed"):
                await middleware.dispatch(request, call_next)
        else:
            await middleware.dispatch(request, call_next)
        assert client_app_context.get() == "outer"
    finally:
        client_app_context.reset(token)


async def test_concurrent_requests_keep_their_own_client_app() -> None:
    middleware = LoggingMiddleware(FastAPI())
    ready = asyncio.Event()
    apps: list[str] = []

    async def call_next(request: Request) -> Response:
        apps.append(request.headers["x-title"])
        if len(apps) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        assert client_app_context.get() == request.headers["x-title"]
        return Response()

    await asyncio.gather(
        *(
            middleware.dispatch(
                Request(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/test",
                        "query_string": b"",
                        "headers": [(b"x-title", app)],
                    }
                ),
                call_next,
            )
            for app in (b"Goose", b"Pi")
        )
    )


def test_filter_defaults_to_unknown_outside_request_context() -> None:
    record = _record()
    assert ClientAppFilter().filter(record) is True
    assert record.client_app == UNKNOWN_CLIENT_APP  # type: ignore[attr-defined]


def test_handler_logs_carry_client_app(caplog: pytest.LogCaptureFixture) -> None:
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
