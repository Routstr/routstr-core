from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from routstr.core.exceptions import EhbpTimeoutError, UpstreamError
from routstr.upstream import ehbp as ehbp_module

# ---------------------------------------------------------------------------
# forward_ehbp_x_cashu_request — timeout fails closed with a refund + 504
# ---------------------------------------------------------------------------


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

    model_obj = MagicMock()
    model_obj.id = "tinfoil-kimi-k2-6"
    model_obj.forwarded_model_id = "kimi-k2-6"
    return upstream, model_obj


@pytest.mark.asyncio
async def test_x_cashu_timeout_refunds_and_returns_504(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ehbp_module,
        "recieve_token",
        AsyncMock(return_value=(1000, "msat", None)),
    )
    monkeypatch.setattr(
        ehbp_module, "store_cashu_transaction", AsyncMock(return_value=None)
    )
    send_cashu_refund_mock = AsyncMock(return_value="refund-token")
    monkeypatch.setattr(ehbp_module, "send_cashu_refund", send_cashu_refund_mock)
    monkeypatch.setattr(
        ehbp_module,
        "forward_with_trailer",
        AsyncMock(side_effect=EhbpTimeoutError("EHBP upstream timed out")),
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

    assert response.status_code == 504
    assert response.headers["X-Cashu"] == "refund-token"
    send_cashu_refund_mock.assert_awaited_once_with(1000, "msat", None, "req-123")


# ---------------------------------------------------------------------------
# forward_ehbp_request — the bearer path must let the timeout through, so
# proxy.py can answer 504 instead of flattening it to a generic 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_timeout_propagates_504(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out bearer request must not be rewritten to a 500.

    ``forward_ehbp_request`` ends in a bare ``except Exception`` that turns any
    error into ``UpstreamError(..., status_code=500)``. The ``except
    UpstreamError: raise`` above it is the only thing preserving the 504 that
    ``proxy.py`` returns to the client, so this test pins that handler.
    """
    monkeypatch.setattr(
        ehbp_module,
        "forward_with_trailer",
        AsyncMock(
            side_effect=EhbpTimeoutError(
                "EHBP upstream inference.tinfoil.sh timed out after 600s connecting"
            )
        ),
    )
    upstream, model_obj = _ehbp_upstream_mocks()
    key = MagicMock()
    key.hashed_key = "abcdef1234567890"

    with pytest.raises(EhbpTimeoutError) as exc_info:
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

    assert exc_info.value.status_code == 504
    assert exc_info.value.code == "UPSTREAM_TIMEOUT"
    assert isinstance(exc_info.value, UpstreamError)
