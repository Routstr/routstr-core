from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from routstr.core.exceptions import EhbpTimeoutError
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
    monkeypatch.setattr(
        ehbp_module, "send_cashu_refund", AsyncMock(return_value="refund-token")
    )
    monkeypatch.setattr(
        ehbp_module,
        "forward_with_trailer",
        AsyncMock(side_effect=EhbpTimeoutError("EHBP upstream timed out")),
    )

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
    ehbp_module.send_cashu_refund.assert_awaited_once_with(
        1000, "msat", None, "req-123"
    )
