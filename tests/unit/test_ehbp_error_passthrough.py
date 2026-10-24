"""Unit tests for EHBP encrypted error-response passthrough.

An EHBP enclave encrypts error bodies end-to-end, so the proxy cannot read
them. Wrapping the ciphertext in a proxy JSON envelope handed callers mojibake
instead of the provider's actual error; these tests pin the relay behaviour
that lets the EHBP client decrypt the real failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from routstr.core.error_scope import (
    ERROR_SCOPE_HEADER,
    ERROR_SCOPE_UPSTREAM,
)
from routstr.core.exceptions import EhbpEncryptedUpstreamError, UpstreamError
from routstr.upstream import ehbp as ehbp_module
from routstr.upstream.ehbp import (
    _is_ehbp_encrypted_response,
    _passthrough_encrypted_response,
)
from routstr.upstream.tinfoil_trailer import TrailerResponse

_CIPHERTEXT = b"\x00\x00\x00\x02cb"
_NONCE = "ab" * 32


def _encrypted_response(
    *, status_code: int = 404, body: bytes = _CIPHERTEXT
) -> TrailerResponse:
    return TrailerResponse(
        status_code=status_code,
        headers=[
            ("content-type", "application/octet-stream"),
            ("ehbp-response-nonce", _NONCE),
            ("tinfoil-enclave", "deepseek-v4-1-flash-inf15-0.tinfoil.dev"),
            ("content-length", str(len(body))),
            ("connection", "keep-alive"),
        ],
        body=body,
    )


def _plaintext_response(
    *, status_code: int = 502, body: bytes = b'{"error":"bad gateway"}'
) -> TrailerResponse:
    return TrailerResponse(
        status_code=status_code,
        headers=[("content-type", "application/json")],
        body=body,
    )


async def _request() -> MagicMock:
    request = MagicMock()
    request.state.request_id = "req-123"
    request.method = "POST"
    request.query_params = {}
    request.headers = {}
    request.body = AsyncMock(return_value=b"opaque")
    return request


def _ehbp_upstream_mocks() -> tuple[MagicMock, MagicMock]:
    """Upstream and model mocks sufficient to reach the forwarding call."""
    profile = MagicMock()
    profile.client_target_url_header = None
    profile.allow_client_target_override = False
    profile.proxy_only_headers = frozenset()
    profile.usage_response_header = None

    target = MagicMock()
    target.url = "https://inference.tinfoil.sh/v1/chat/completions"
    target.headers = {}
    target.profile = None

    upstream = MagicMock()
    upstream.prepare_headers.return_value = {}
    upstream.get_ehbp_forwarding_target.return_value = target
    upstream.get_confidential_inference_profile.return_value = profile
    upstream.prepare_params.return_value = {}
    upstream.provider_type = "tinfoil"

    model_obj = MagicMock()
    model_obj.id = "tinfoil-kimi-k2-6"
    model_obj.forwarded_model_id = "kimi-k2-6"
    return upstream, model_obj


# ---------------------------------------------------------------------------
# Detection + reconstruction helpers
# ---------------------------------------------------------------------------


class TestIsEhbpEncryptedResponse:
    def test_nonce_marks_response_as_encrypted(self) -> None:
        assert _is_ehbp_encrypted_response(_encrypted_response()) is True

    def test_plaintext_error_has_no_nonce(self) -> None:
        assert _is_ehbp_encrypted_response(_plaintext_response()) is False

    def test_blank_nonce_is_not_encrypted(self) -> None:
        resp = TrailerResponse(
            status_code=502,
            headers=[("ehbp-response-nonce", "   ")],
            body=b"",
        )
        assert _is_ehbp_encrypted_response(resp) is False


class TestPassthroughEncryptedResponse:
    def test_preserves_status_body_and_nonce(self) -> None:
        response = _passthrough_encrypted_response(_encrypted_response())

        assert response.status_code == 404
        assert bytes(response.body) == _CIPHERTEXT
        assert response.headers["ehbp-response-nonce"] == _NONCE
        # Enclave diagnostics survive so clients can verify who answered.
        assert response.headers["tinfoil-enclave"].startswith("deepseek")

    def test_drops_connection_framing_and_recomputes_length(self) -> None:
        response = _passthrough_encrypted_response(_encrypted_response())

        assert "connection" not in response.headers
        # The proxy re-frames the body, so the upstream length is meaningless.
        assert response.headers["content-length"] == str(len(_CIPHERTEXT))

    def test_marks_error_scope_as_upstream(self) -> None:
        response = _passthrough_encrypted_response(_encrypted_response())

        assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM

    def test_error_scope_cannot_be_spoofed_by_the_enclave(self) -> None:
        resp = _encrypted_response()
        resp.headers.append((ERROR_SCOPE_HEADER, "node"))

        response = _passthrough_encrypted_response(resp)

        assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM


# ---------------------------------------------------------------------------
# forward_ehbp_request (bearer path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_encrypted_error_is_handed_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An encrypted non-200 must keep its ciphertext and nonce for decoding."""
    monkeypatch.setattr(
        ehbp_module,
        "forward_with_trailer",
        AsyncMock(return_value=_encrypted_response()),
    )
    upstream, model_obj = _ehbp_upstream_mocks()
    key = MagicMock()
    key.hashed_key = "abcdef1234567890"

    with pytest.raises(EhbpEncryptedUpstreamError) as exc_info:
        await ehbp_module.forward_ehbp_request(
            request=await _request(),
            path="v1/chat/completions",
            headers={},
            request_body=b"opaque",
            upstream=upstream,
            key=key,
            max_cost_for_model=5000,
            session=MagicMock(),
            model_obj=model_obj,
        )

    error = exc_info.value
    assert error.status_code == 404
    assert error.from_upstream_response is True
    assert error.scope == ERROR_SCOPE_UPSTREAM
    assert isinstance(error, UpstreamError)
    assert error.response.status_code == 404
    assert bytes(error.response.body) == _CIPHERTEXT
    assert error.response.headers["ehbp-response-nonce"] == _NONCE
    # The ciphertext must not leak into the message that ends up in logs.
    assert "\\x00" not in error.message
    assert "EHBP ciphertext" not in error.message
    assert "404" in error.message


@pytest.mark.asyncio
async def test_bearer_plaintext_error_keeps_the_proxy_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-encrypted non-200 still surfaces as a structured upstream error."""
    monkeypatch.setattr(
        ehbp_module,
        "forward_with_trailer",
        AsyncMock(return_value=_plaintext_response()),
    )
    upstream, model_obj = _ehbp_upstream_mocks()
    key = MagicMock()
    key.hashed_key = "abcdef1234567890"

    with pytest.raises(UpstreamError) as exc_info:
        await ehbp_module.forward_ehbp_request(
            request=await _request(),
            path="v1/chat/completions",
            headers={},
            request_body=b"opaque",
            upstream=upstream,
            key=key,
            max_cost_for_model=5000,
            session=MagicMock(),
            model_obj=model_obj,
        )

    error = exc_info.value
    assert not isinstance(error, EhbpEncryptedUpstreamError)
    assert error.from_upstream_response is True
    assert "bad gateway" in error.message


# ---------------------------------------------------------------------------
# forward_ehbp_x_cashu_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_x_cashu_encrypted_error_is_relayed_with_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The X-Cashu path refunds and still lets the client decrypt the error."""
    monkeypatch.setattr(
        ehbp_module, "recieve_token", AsyncMock(return_value=(1000, "msat", None))
    )
    monkeypatch.setattr(
        ehbp_module, "store_cashu_transaction", AsyncMock(return_value=None)
    )
    send_cashu_refund_mock = AsyncMock(return_value="refund-token")
    monkeypatch.setattr(ehbp_module, "send_cashu_refund", send_cashu_refund_mock)
    monkeypatch.setattr(
        ehbp_module,
        "forward_with_trailer",
        AsyncMock(return_value=_encrypted_response(status_code=400)),
    )
    upstream, model_obj = _ehbp_upstream_mocks()

    response = await ehbp_module.forward_ehbp_x_cashu_request(
        request=await _request(),
        x_cashu_token="cashu-token",
        path="v1/chat/completions",
        max_cost_for_model=5000,
        model_obj=model_obj,
        upstream=upstream,
    )

    assert response.status_code == 400
    assert bytes(response.body) == _CIPHERTEXT
    assert response.headers["ehbp-response-nonce"] == _NONCE
    assert response.headers["X-Cashu"] == "refund-token"
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    send_cashu_refund_mock.assert_awaited_once_with(1000, "msat", None, "req-123")
