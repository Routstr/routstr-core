"""Unit tests for client-app identification in request logging."""

import logging

from starlette.datastructures import Headers

from routstr.core.logging import ClientAppFilter
from routstr.core.middleware import (
    UNKNOWN_CLIENT_APP,
    client_app_context,
    client_app_from_headers,
)


def _headers(**kwargs: str) -> Headers:
    return Headers({k.replace("_", "-"): v for k, v in kwargs.items()})


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


class TestClientAppFromHeaders:
    def test_x_title_wins_over_all(self) -> None:
        headers = _headers(
            x_title="Goose",
            referer="https://myapp.example.com",
            user_agent="python-httpx/0.27",
        )
        assert client_app_from_headers(headers) == "Goose"

    def test_referer_used_when_no_x_title(self) -> None:
        headers = _headers(
            referer="https://myapp.example.com", user_agent="python-httpx/0.27"
        )
        assert client_app_from_headers(headers) == "https://myapp.example.com"

    def test_user_agent_is_last_fallback(self) -> None:
        headers = _headers(user_agent="python-httpx/0.27")
        assert client_app_from_headers(headers) == "python-httpx/0.27"

    def test_unknown_when_no_identity_headers(self) -> None:
        assert client_app_from_headers(Headers({})) == UNKNOWN_CLIENT_APP
        assert UNKNOWN_CLIENT_APP == "unknown"

    def test_blank_header_falls_through_to_next(self) -> None:
        headers = _headers(x_title="   ", user_agent="curl/8.4.0")
        assert client_app_from_headers(headers) == "curl/8.4.0"

    def test_all_blank_resolves_to_unknown(self) -> None:
        headers = _headers(x_title="   ", user_agent="\t")
        assert client_app_from_headers(headers) == UNKNOWN_CLIENT_APP

    def test_value_is_truncated(self) -> None:
        headers = _headers(x_title="a" * 500)
        assert client_app_from_headers(headers) == "a" * 120

    def test_control_characters_are_stripped(self) -> None:
        # A crafted header must not be able to inject fake log records.
        headers = _headers(user_agent="evil-app\x1b[0m fake INFO line")
        assert client_app_from_headers(headers) == "evil-app[0m fake INFO line"


class TestClientAppFilter:
    def test_uses_context_variable(self) -> None:
        token = client_app_context.set("Goose")
        try:
            record = _record()
            assert ClientAppFilter().filter(record) is True
            assert record.client_app == "Goose"  # type: ignore[attr-defined]
        finally:
            client_app_context.reset(token)

    def test_defaults_to_unknown_outside_request_context(self) -> None:
        record = _record()
        assert ClientAppFilter().filter(record) is True
        assert record.client_app == UNKNOWN_CLIENT_APP  # type: ignore[attr-defined]

    def test_explicit_extra_is_not_overwritten(self) -> None:
        token = client_app_context.set("context-app")
        try:
            record = _record()
            record.client_app = "explicit-app"  # type: ignore[attr-defined]
            assert ClientAppFilter().filter(record) is True
            assert record.client_app == "explicit-app"  # type: ignore[attr-defined]
        finally:
            client_app_context.reset(token)
