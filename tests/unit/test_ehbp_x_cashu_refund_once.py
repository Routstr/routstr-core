"""An EHBP X-Cashu request refunds at most once.

Minting a refund token can consume proofs before its ledger write fails, so
the error handler must not mint a second, full refund for the same request.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import Response

from routstr.core.error_scope import (
    ERROR_SCOPE_HEADER,
    ERROR_SCOPE_NODE,
    ERROR_SCOPE_UPSTREAM,
)
from routstr.upstream import ehbp as ehbp_module
from routstr.upstream.tinfoil_trailer import TrailerResponse

from .test_ehbp_error_passthrough import _ehbp_upstream_mocks, _request


def _ok_response() -> TrailerResponse:
    return TrailerResponse(
        status_code=200,
        headers=[("content-type", "application/octet-stream")],
        body=b"ciphertext",
    )


def _error_response() -> TrailerResponse:
    return TrailerResponse(
        status_code=502,
        headers=[("content-type", "application/json")],
        body=b'{"error":"bad gateway"}',
    )


def _wallet(
    monkeypatch: pytest.MonkeyPatch, *, mint_fails: bool, store_fails: bool
) -> AsyncMock:
    monkeypatch.setattr(
        ehbp_module, "recieve_token", AsyncMock(return_value=(1000, "msat", None))
    )
    minted = AsyncMock(
        side_effect=RuntimeError("mint unreachable") if mint_fails else None,
        return_value="minted-refund",
    )
    monkeypatch.setattr(ehbp_module, "send_token", minted)

    async def store(**kwargs: object) -> None:
        if store_fails and kwargs["typ"] == "out":
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(ehbp_module, "store_cashu_transaction", store)
    return minted


async def _forward(
    monkeypatch: pytest.MonkeyPatch, upstream_response: object
) -> Response:
    monkeypatch.setattr(
        ehbp_module, "forward_with_trailer", AsyncMock(return_value=upstream_response)
    )
    monkeypatch.setattr(
        ehbp_module,
        "_compute_ehbp_actual_cost",
        AsyncMock(
            return_value={
                "total_msats": 400,
                "computed_msats": 400,
                "input_msats": 100,
                "output_msats": 300,
            }
        ),
    )
    upstream, model_obj = _ehbp_upstream_mocks()
    return await ehbp_module.forward_ehbp_x_cashu_request(
        request=await _request(),
        x_cashu_token="cashu-token",
        path="v1/chat/completions",
        max_cost_for_model=1000,
        model_obj=model_obj,
        upstream=upstream,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_ok", [True, False])
async def test_refund_whose_ledger_write_fails_is_not_minted_again(
    monkeypatch: pytest.MonkeyPatch, upstream_ok: bool
) -> None:
    minted = _wallet(monkeypatch, mint_fails=False, store_fails=True)

    response = await _forward(
        monkeypatch, _ok_response() if upstream_ok else _error_response()
    )

    minted.assert_awaited_once()
    assert minted.await_args is not None
    assert minted.await_args.args[0] == (600 if upstream_ok else 1000)
    # The token already minted still reaches the client.
    assert response.headers["X-Cashu"] == "minted-refund"
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM


@pytest.mark.asyncio
async def test_failed_refund_mint_is_reported_without_a_second_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minted = _wallet(monkeypatch, mint_fails=True, store_fails=False)

    response = await _forward(monkeypatch, _ok_response())

    minted.assert_awaited_once()
    assert response.status_code == 500
    assert "X-Cashu" not in response.headers
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_NODE
    assert json.loads(bytes(response.body))["error"]["code"] == "refund_failed"


@pytest.mark.asyncio
async def test_error_before_any_refund_still_refunds_in_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minted = _wallet(monkeypatch, mint_fails=False, store_fails=False)
    monkeypatch.setattr(
        ehbp_module,
        "_compute_ehbp_actual_cost",
        AsyncMock(side_effect=RuntimeError("bad usage")),
    )
    monkeypatch.setattr(
        ehbp_module, "forward_with_trailer", AsyncMock(return_value=_ok_response())
    )
    upstream, model_obj = _ehbp_upstream_mocks()

    response = await ehbp_module.forward_ehbp_x_cashu_request(
        request=await _request(),
        x_cashu_token="cashu-token",
        path="v1/chat/completions",
        max_cost_for_model=1000,
        model_obj=model_obj,
        upstream=upstream,
    )

    minted.assert_awaited_once()
    assert minted.await_args is not None
    assert minted.await_args.args[0] == 1000
    assert response.headers["X-Cashu"] == "minted-refund"


@pytest.mark.asyncio
async def test_error_whose_full_refund_fails_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minted = _wallet(monkeypatch, mint_fails=True, store_fails=False)
    monkeypatch.setattr(
        ehbp_module,
        "_compute_ehbp_actual_cost",
        AsyncMock(side_effect=RuntimeError("bad usage")),
    )
    monkeypatch.setattr(
        ehbp_module, "forward_with_trailer", AsyncMock(return_value=_ok_response())
    )
    upstream, model_obj = _ehbp_upstream_mocks()

    response = await ehbp_module.forward_ehbp_x_cashu_request(
        request=await _request(),
        x_cashu_token="cashu-token",
        path="v1/chat/completions",
        max_cost_for_model=1000,
        model_obj=model_obj,
        upstream=upstream,
    )

    minted.assert_awaited_once()
    assert response.status_code == 500
    assert json.loads(bytes(response.body))["error"]["code"] == "refund_failed"
