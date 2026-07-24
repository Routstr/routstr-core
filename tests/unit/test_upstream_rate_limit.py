"""Tests for upstream rate-limit detection, classification, and org-ID redaction.

Covers issue #555: upstream OpenAI-compatible providers return rate-limit
errors that embed a sensitive organization ID. The proxy must classify these
distinctly (``UPSTREAM_RATE_LIMIT``), preserve useful debugging fields, and
never emit a raw ``org-*`` identifier in logs, errors, or returned bodies.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest

from routstr.core.redaction import redact_org_ids
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.rate_limit import (
    UPSTREAM_RATE_LIMIT,
    RateLimitInfo,
    classify_rate_limit,
)

# The exact scenario from the issue, with a realistic (fake) org identifier.
RAW_ORG_ID = "org-abc123XYZ456def"
RATE_LIMIT_MESSAGE = (
    f"Rate limit reached for gpt-5.5-2026-04-23 (for limit gpt-5.5) in "
    f"organization {RAW_ORG_ID} on tokens per min (TPM): Limit 180000000, "
    f"Used 180000000, Requested 8929. Please try again in 2ms. Visit "
    f"https://platform.openai.com/account/rate-limits to learn more."
)


def _make_request(request_id: str = "req-123") -> Mock:
    request = Mock(spec=["method", "state"])
    request.method = "POST"
    request.state = Mock()
    request.state.request_id = request_id
    return request


def _make_upstream_response(
    *,
    body: bytes,
    status_code: int = 429,
    content_type: str | None = "application/json",
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


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_redact_org_ids_replaces_identifier() -> None:
    assert RAW_ORG_ID not in redact_org_ids(RATE_LIMIT_MESSAGE)
    assert "org-[REDACTED]" in redact_org_ids(RATE_LIMIT_MESSAGE)


def test_redact_org_ids_is_idempotent() -> None:
    once = redact_org_ids(RATE_LIMIT_MESSAGE)
    assert redact_org_ids(once) == once


def test_redact_org_ids_leaves_unrelated_text() -> None:
    assert redact_org_ids("organize the org-chart") == "organize the org-chart"
    assert redact_org_ids("") == ""


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def test_classify_exact_scenario() -> None:
    info = classify_rate_limit(429, RATE_LIMIT_MESSAGE)
    assert isinstance(info, RateLimitInfo)
    assert info.code == UPSTREAM_RATE_LIMIT
    assert info.model == "gpt-5.5-2026-04-23"
    assert info.limit_name == "gpt-5.5"
    assert info.metric == "tokens per min (TPM)"
    assert info.limit == 180000000
    assert info.used == 180000000
    assert info.requested == 8929
    assert info.retry_after_seconds == pytest.approx(0.002)
    # Redaction-safe: no raw org id survives into the structured view.
    assert RAW_ORG_ID not in info.message
    assert RAW_ORG_ID not in json.dumps(info.as_details())


def test_classify_by_status_code_without_marker() -> None:
    info = classify_rate_limit(429, "slow down")
    assert info is not None
    assert info.code == UPSTREAM_RATE_LIMIT


def test_classify_by_message_marker_without_429() -> None:
    info = classify_rate_limit(400, "rate_limit_exceeded for this key")
    assert info is not None


def test_retry_after_header_takes_precedence() -> None:
    info = classify_rate_limit(429, RATE_LIMIT_MESSAGE, {"Retry-After": "12"})
    assert info is not None
    assert info.retry_after_seconds == pytest.approx(12.0)


def test_non_rate_limit_error_is_not_classified() -> None:
    assert classify_rate_limit(400, "invalid request: missing field 'model'") is None
    assert classify_rate_limit(500, "internal server error") is None


# --------------------------------------------------------------------------- #
# forward_upstream_error_response integration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_json_rate_limit_body_is_redacted_and_forwarded(
    provider: BaseUpstreamProvider,
) -> None:
    body = json.dumps(
        {"error": {"message": RATE_LIMIT_MESSAGE, "type": "rate_limit_exceeded"}}
    ).encode()
    upstream = _make_upstream_response(body=body, status_code=429)

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/chat/completions", upstream
    )

    assert response.status_code == 429
    raw = bytes(response.body).decode()
    # No raw organization id may survive in the forwarded body.
    assert RAW_ORG_ID not in raw
    assert "org-[REDACTED]" in raw
    # Body remains valid JSON; the original type is preserved while a stable
    # rate-limit code is injected so callers can switch on it.
    payload: dict[str, Any] = json.loads(raw)
    assert payload["error"]["type"] == "rate_limit_exceeded"
    assert payload["error"]["code"] == UPSTREAM_RATE_LIMIT
    assert payload["error"]["details"]["model"] == "gpt-5.5-2026-04-23"
    # A retry hint extracted from the message is surfaced as a header.
    assert "retry-after" in {k.lower() for k in response.headers}


@pytest.mark.asyncio
async def test_non_json_rate_limit_envelope_uses_stable_code(
    provider: BaseUpstreamProvider,
) -> None:
    upstream = _make_upstream_response(
        body=RATE_LIMIT_MESSAGE.encode(),
        status_code=429,
        content_type="text/plain",
    )

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/chat/completions", upstream
    )

    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["code"] == UPSTREAM_RATE_LIMIT
    assert payload["error"]["details"]["model"] == "gpt-5.5-2026-04-23"
    serialized = json.dumps(payload)
    assert RAW_ORG_ID not in serialized
    assert "org-[REDACTED]" in serialized


@pytest.mark.asyncio
async def test_non_rate_limit_json_error_unchanged(
    provider: BaseUpstreamProvider,
) -> None:
    body = json.dumps(
        {"error": {"message": "missing field 'model'", "type": "invalid_request"}}
    ).encode()
    upstream = _make_upstream_response(body=body, status_code=400)

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/chat/completions", upstream
    )

    assert response.status_code == 400
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["type"] == "invalid_request"
    assert "retry-after" not in {k.lower() for k in response.headers}


# --------------------------------------------------------------------------- #
# UpstreamError -> proxy response (preserves code/details/status)
# --------------------------------------------------------------------------- #


def test_create_upstream_error_response_preserves_structure() -> None:
    from routstr.core.exceptions import UpstreamError
    from routstr.payment.helpers import create_upstream_error_response

    info = classify_rate_limit(429, RATE_LIMIT_MESSAGE)
    assert info is not None
    err = UpstreamError(
        f"Upstream error via litellm: {RATE_LIMIT_MESSAGE}",
        status_code=429,
        code=info.code,
        details=info.as_details(),
    )

    response = create_upstream_error_response(err, _make_request())

    # Original upstream status is preserved (not flattened to 502).
    assert response.status_code == 429
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["code"] == UPSTREAM_RATE_LIMIT
    assert payload["error"]["details"]["requested"] == 8929
    serialized = json.dumps(payload)
    assert RAW_ORG_ID not in serialized
    assert "org-[REDACTED]" in serialized


def test_generic_upstream_error_still_defaults_to_502() -> None:
    from routstr.core.exceptions import UpstreamError
    from routstr.payment.helpers import create_upstream_error_response

    err = UpstreamError("connection refused")  # status_code defaults to 502

    response = create_upstream_error_response(err, _make_request())

    assert response.status_code == 502
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["code"] == 502
    assert "details" not in payload["error"]


# --------------------------------------------------------------------------- #
# Structured log-extra redaction
# --------------------------------------------------------------------------- #


def test_security_filter_redacts_org_id_in_extra() -> None:
    import logging

    from routstr.core.logging import SecurityFilter

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="upstream failed",
        args=(),
        exc_info=None,
    )
    # Simulate an ``extra={"body_preview": ...}`` field carrying an org id.
    setattr(record, "body_preview", RATE_LIMIT_MESSAGE)

    assert SecurityFilter().filter(record) is True
    redacted: str = getattr(record, "body_preview")
    assert RAW_ORG_ID not in redacted
    assert "org-[REDACTED]" in redacted


def test_security_filter_redacts_org_id_in_nested_extra() -> None:
    import logging

    from routstr.core.logging import SecurityFilter

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="upstream failed",
        args=(),
        exc_info=None,
    )
    # Nested structures: dict containing a list containing the org id.
    setattr(record, "body", {"error": {"messages": [RATE_LIMIT_MESSAGE]}})

    assert SecurityFilter().filter(record) is True
    serialized = json.dumps(getattr(record, "body"))
    assert RAW_ORG_ID not in serialized
    assert "org-[REDACTED]" in serialized


# --------------------------------------------------------------------------- #
# 5xx-wrapped rate limit through forward_upstream_error_response
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_5xx_wrapped_rate_limit_is_classified(
    provider: BaseUpstreamProvider,
) -> None:
    # Some providers wrap a rate-limit in a 5xx envelope; classification must
    # key off the message marker, not only the 429 status.
    body = json.dumps({"error": {"message": RATE_LIMIT_MESSAGE}}).encode()
    upstream = _make_upstream_response(body=body, status_code=500)

    response = await provider.forward_upstream_error_response(
        _make_request(), "v1/chat/completions", upstream
    )

    assert response.status_code == 500
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["code"] == UPSTREAM_RATE_LIMIT
    serialized = json.dumps(payload)
    assert RAW_ORG_ID not in serialized
    assert "org-[REDACTED]" in serialized


# --------------------------------------------------------------------------- #
# Real proxy loop: structured error surfaced + reservation reverted once
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_proxy_loop_surfaces_rate_limit_and_reverts_once() -> None:
    from routstr import proxy as proxy_module
    from routstr.core.db import ApiKey
    from routstr.core.exceptions import UpstreamError

    info = classify_rate_limit(429, RATE_LIMIT_MESSAGE)
    assert info is not None

    key = ApiKey(hashed_key="rlkey", balance=10_000)

    request = MagicMock()
    request.method = "POST"
    request.headers = {"authorization": "Bearer sk-rlkey"}
    request.body = AsyncMock(return_value=b'{"model": "test-model"}')
    request.state = MagicMock()
    request.state.request_id = "req-rl"

    upstream = MagicMock()
    upstream.provider_type = "test"
    upstream.prepare_headers = MagicMock(side_effect=lambda h: h)
    upstream.forward_request = AsyncMock(
        side_effect=UpstreamError(
            f"Upstream error via litellm: {RATE_LIMIT_MESSAGE}",
            status_code=429,
            code=info.code,
            details=info.as_details(),
        )
    )

    session = MagicMock()
    revert_mock = AsyncMock(return_value=True)

    with (
        patch.object(
            proxy_module,
            "get_candidates",
            return_value=[(MagicMock(), upstream)],
        ),
        patch.object(
            proxy_module, "get_max_cost_for_model", AsyncMock(return_value=1_000)
        ),
        patch.object(
            proxy_module,
            "calculate_discounted_max_cost",
            AsyncMock(return_value=1_000),
        ),
        patch.object(proxy_module, "check_token_balance", MagicMock()),
        patch.object(
            proxy_module, "get_bearer_token_key", AsyncMock(return_value=key)
        ),
        patch.object(proxy_module, "pay_for_request", AsyncMock(return_value=1_000)),
        patch.object(proxy_module, "revert_pay_for_request", revert_mock),
    ):
        response = await proxy_module.proxy(
            request, "v1/chat/completions", session=session
        )

    # Original 429 status and the stable code/details survive to the client.
    assert response.status_code == 429
    payload: dict[str, Any] = json.loads(bytes(response.body))
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["code"] == UPSTREAM_RATE_LIMIT
    assert payload["error"]["details"]["requested"] == 8929
    serialized = json.dumps(payload)
    assert RAW_ORG_ID not in serialized
    assert "org-[REDACTED]" in serialized
    # Single upstream failed -> reservation reverted exactly once (no double-charge).
    revert_mock.assert_awaited_once_with(key, session, 1_000)
