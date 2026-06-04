"""Tests for ``BaseUpstreamProvider.forward_upstream_error_response``.

Upstream services (e.g. an Express server that doesn't expose ``/messages``)
sometimes return a non-JSON error body. The proxy must surface those errors
in a consistent JSON envelope so clients don't have to parse HTML.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from routstr.upstream.base import BaseUpstreamProvider, _is_json_content_type


def _make_request(request_id: str = "req-123") -> Mock:
    request = Mock(spec=["method", "state"])
    request.method = "POST"
    request.state = Mock()
    request.state.request_id = request_id
    return request


def _make_upstream_response(
    *,
    body: bytes,
    status_code: int = 404,
    content_type: str | None = "text/html",
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {}
    if content_type is not None:
        headers["content-type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    return httpx.Response(status_code=status_code, headers=headers, content=body)


@pytest.fixture
def provider() -> BaseUpstreamProvider:
    return BaseUpstreamProvider(
        base_url="https://privateprovider.xyz", api_key="k", provider_fee=1.0
    )


@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("application/json", True),
        ("application/json; charset=utf-8", True),
        ("text/json", True),
        ("application/problem+json", True),
        ("application/vnd.api+json", True),
        ("text/html", False),
        ("text/html; charset=utf-8", False),
        ("text/plain", False),
        ("", False),
        (None, False),
    ],
)
def test_is_json_content_type(content_type: str | None, expected: bool) -> None:
    assert _is_json_content_type(content_type) is expected


@pytest.mark.asyncio
async def test_html_error_is_normalized_to_json_envelope(
    provider: BaseUpstreamProvider,
) -> None:
    html_body = (
        b"<!DOCTYPE html><html><head><title>Error</title></head>"
        b"<body><pre>Cannot POST /messages</pre></body></html>"
    )
    upstream = _make_upstream_response(body=html_body, status_code=404)

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/messages", upstream
    )

    assert response.status_code == 404
    assert response.media_type == "application/json"
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["upstream_status"] == 404
    assert payload["error"]["upstream_content_type"] == "text/html"
    assert "Cannot POST /messages" in payload["error"]["upstream_body_preview"]
    assert payload["request_id"] == "req-123"
    # The upstream's text/html content-type must not survive — Response()
    # sets the JSON content-type for us via media_type.
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_plain_text_error_is_normalized(
    provider: BaseUpstreamProvider,
) -> None:
    upstream = _make_upstream_response(
        body=b"Service Unavailable", status_code=503, content_type="text/plain"
    )

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/messages", upstream
    )

    assert response.status_code == 503
    assert response.media_type == "application/json"
    payload = json.loads(bytes(response.body))
    assert payload["error"]["message"] == "Service Unavailable"


@pytest.mark.asyncio
async def test_empty_body_with_non_json_content_type_normalizes(
    provider: BaseUpstreamProvider,
) -> None:
    upstream = _make_upstream_response(
        body=b"", status_code=502, content_type="text/html"
    )

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/messages", upstream
    )

    assert response.status_code == 502
    assert response.media_type == "application/json"
    payload = json.loads(bytes(response.body))
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["upstream_body_preview"] is None


@pytest.mark.asyncio
async def test_json_error_body_is_passed_through_unchanged(
    provider: BaseUpstreamProvider,
) -> None:
    json_body = json.dumps(
        {"error": {"message": "Invalid model", "type": "invalid_request_error"}}
    ).encode()
    upstream = _make_upstream_response(
        body=json_body, status_code=400, content_type="application/json"
    )

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/messages", upstream
    )

    assert response.status_code == 400
    assert bytes(response.body) == json_body
    assert response.media_type == "application/json"
