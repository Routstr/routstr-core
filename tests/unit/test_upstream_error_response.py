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

from routstr.core.error_scope import (
    ERROR_SCOPE_HEADER,
    ERROR_SCOPE_NODE,
    ERROR_SCOPE_UPSTREAM,
    UPSTREAM_ERROR_STATUS,
    UPSTREAM_UNAVAILABLE,
    client_code_for_upstream_error,
    client_status_for_upstream_error,
)
from routstr.core.exceptions import UpstreamError
from routstr.payment.helpers import create_upstream_error_response
from routstr.upstream.base import BaseUpstreamProvider, _is_json_content_type
from routstr.upstream.rate_limit import UPSTREAM_RATE_LIMIT


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

    assert response.status_code == UPSTREAM_ERROR_STATUS
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    assert response.media_type == "application/json"
    payload = json.loads(bytes(response.body))
    assert payload["error"]["message"] == "Service Unavailable"
    assert payload["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert payload["error"]["upstream_status"] == 503


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

    assert response.status_code == UPSTREAM_ERROR_STATUS
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    assert response.media_type == "application/json"
    payload = json.loads(bytes(response.body))
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert payload["error"]["upstream_status"] == 502
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


# --------------------------------------------------------------------------- #
# Upstream 5xx -> 424 + UPSTREAM_UNAVAILABLE + scope header; node faults stay
# 500 without it; rate limits keep 429.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1/chat/completions", "v1/messages", "v1/responses"])
@pytest.mark.parametrize("upstream_status", [500, 502, 503, 504])
async def test_upstream_5xx_is_attributed_to_the_upstream(
    provider: BaseUpstreamProvider, path: str, upstream_status: int
) -> None:
    body = json.dumps(
        {"error": {"message": "provider exploded", "type": "server_error"}}
    ).encode()
    upstream = _make_upstream_response(
        body=body, status_code=upstream_status, content_type="application/json"
    )

    response = await provider.forward_upstream_error_response(
        _make_request(), path, upstream
    )

    assert response.status_code == UPSTREAM_ERROR_STATUS
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert payload["error"]["upstream_status"] == upstream_status


@pytest.mark.asyncio
async def test_upstream_5xx_non_json_body_keeps_scope_and_status(
    provider: BaseUpstreamProvider,
) -> None:
    """The envelope for a non-JSON 5xx carries the same attribution."""
    upstream = _make_upstream_response(
        body=b"<html>bad gateway</html>", status_code=502, content_type="text/html"
    )

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/chat/completions", upstream
    )

    assert response.status_code == UPSTREAM_ERROR_STATUS
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert payload["error"]["upstream_status"] == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [400, 401, 403, 404, 422])
async def test_provider_4xx_passes_through_unchanged(
    provider: BaseUpstreamProvider, upstream_status: int
) -> None:
    """A provider 4xx is its verdict on the request, not a node-health signal."""
    body = json.dumps(
        {"error": {"message": "bad request", "type": "invalid_request_error"}}
    ).encode()
    upstream = _make_upstream_response(
        body=body, status_code=upstream_status, content_type="application/json"
    )

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/chat/completions", upstream
    )

    assert response.status_code == upstream_status


@pytest.mark.asyncio
async def test_upstream_rate_limit_keeps_429(
    provider: BaseUpstreamProvider,
) -> None:
    """429 + UPSTREAM_RATE_LIMIT is unchanged by the 424 mapping: the retry
    hint is worth more than the status class."""
    body = json.dumps(
        {"error": {"message": "Rate limit reached, please try again"}}
    ).encode()
    upstream = _make_upstream_response(body=body, status_code=429)

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/chat/completions", upstream
    )

    assert response.status_code == 429
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["code"] == UPSTREAM_RATE_LIMIT


def test_generic_upstream_error_response_reports_424() -> None:
    """``create_upstream_error_response`` maps a plain upstream failure to 424."""
    err = UpstreamError("connection refused", status_code=502)

    response = create_upstream_error_response(err, _make_request())

    assert response.status_code == UPSTREAM_ERROR_STATUS
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert payload["error"]["details"]["upstream_status"] == 502


def test_rate_limit_error_response_keeps_429_and_code() -> None:
    err = UpstreamError(
        "slow down", status_code=429, code=UPSTREAM_RATE_LIMIT, details={"a": 1}
    )

    response = create_upstream_error_response(err, _make_request())

    assert response.status_code == 429
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["code"] == UPSTREAM_RATE_LIMIT
    assert payload["error"]["details"] == {"a": 1}


def test_5xx_wrapped_rate_limit_error_response_keeps_429() -> None:
    """A rate limit wrapped in a provider 5xx still answers 429."""
    err = UpstreamError("slow down", status_code=500, code=UPSTREAM_RATE_LIMIT)

    response = create_upstream_error_response(err, _make_request())

    assert response.status_code == 429
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["code"] == UPSTREAM_RATE_LIMIT


def test_node_scoped_failure_stays_500_without_scope_header() -> None:
    """A genuine node fault must never be disguised as an upstream one."""
    err = UpstreamError("mint unreachable", status_code=500, scope=ERROR_SCOPE_NODE)

    response = create_upstream_error_response(err, _make_request())

    assert response.status_code == 500
    assert ERROR_SCOPE_HEADER not in response.headers
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["code"] != UPSTREAM_UNAVAILABLE


def test_upstream_error_defaults_to_upstream_scope() -> None:
    assert UpstreamError("boom").scope == ERROR_SCOPE_UPSTREAM


@pytest.mark.asyncio
async def test_json_body_without_error_mapping_gets_classification(
    provider: BaseUpstreamProvider,
) -> None:
    """A rewritten status is never served without a matching ``error.code``."""
    body = json.dumps({"detail": "internal failure"}).encode()
    upstream = _make_upstream_response(
        body=body, status_code=503, content_type="application/json"
    )

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/chat/completions", upstream
    )

    assert response.status_code == UPSTREAM_ERROR_STATUS
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["detail"] == "internal failure"
    assert payload["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert payload["error"]["upstream_status"] == 503


@pytest.mark.asyncio
async def test_json_body_with_non_mapping_error_is_left_alone(
    provider: BaseUpstreamProvider,
) -> None:
    """A provider's own ``error`` value is never clobbered by the mapping."""
    body = json.dumps({"error": "boom"}).encode()
    upstream = _make_upstream_response(
        body=body, status_code=503, content_type="application/json"
    )

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/chat/completions", upstream
    )

    assert response.status_code == UPSTREAM_ERROR_STATUS
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    assert json.loads(bytes(response.body)) == {"error": "boom"}


@pytest.mark.parametrize("upstream_status", [429, 500, 502, 503, 529])
def test_rate_limit_status_and_code_never_disagree(upstream_status: int) -> None:
    """429 and ``UPSTREAM_RATE_LIMIT`` are one classification, not two: a caller
    must never see ``424`` carrying the rate-limit code."""
    assert client_status_for_upstream_error(upstream_status, UPSTREAM_RATE_LIMIT) == 429
    assert (
        client_code_for_upstream_error(upstream_status, UPSTREAM_RATE_LIMIT)
        == UPSTREAM_RATE_LIMIT
    )


@pytest.mark.parametrize("upstream_status", [400, 401, 403, 404, 422])
def test_provider_4xx_keeps_its_numeric_code(upstream_status: int) -> None:
    """The x-cashu envelopes pass the status as the code; a 4xx must keep the
    legacy numeric ``error.code`` rather than degrade to ``null``."""
    assert client_status_for_upstream_error(upstream_status) == upstream_status
    assert (
        client_code_for_upstream_error(upstream_status, upstream_status)
        == upstream_status
    )
