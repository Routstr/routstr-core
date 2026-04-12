import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import JSONResponse

from routstr.balance import refund_wallet_endpoint
from routstr.core.db import ApiKey, CashuTransaction


def _make_cashu_tx(
    token: str,
    amount: int,
    unit: str,
    type: str = "out",
    request_id: str | None = "req-abc",
    swept: bool = False,
    collected: bool = False,
) -> CashuTransaction:
    tx = CashuTransaction(token=token, amount=amount, unit=unit, type=type, request_id=request_id)
    tx.swept = swept
    tx.collected = collected
    return tx


def _exec_result(tx: CashuTransaction | None) -> MagicMock:
    result = MagicMock()
    result.first.return_value = tx
    return result


@pytest.mark.asyncio
async def test_refund_x_cashu_returns_token() -> None:
    x_cashu_token = "cashuAtest_token_value"
    in_tx = _make_cashu_tx(token=x_cashu_token, amount=0, unit="msat", type="in", request_id="req-abc")
    out_tx = _make_cashu_tx(token="cashuArefund_token", amount=1000, unit="msat", type="out", request_id="req-abc")

    session = MagicMock()
    session.exec = AsyncMock(side_effect=[_exec_result(in_tx), _exec_result(out_tx)])
    session.add = MagicMock()
    session.commit = AsyncMock()

    result = await refund_wallet_endpoint(
        authorization="Bearer sk-somekey",
        x_cashu=x_cashu_token,
        session=session,
    )

    assert isinstance(result, JSONResponse)
    body = json.loads(result.body)
    assert body["token"] == "cashuArefund_token"
    assert body["msats"] == "1000"
    assert result.headers["X-Cashu"] == "cashuArefund_token"
    assert out_tx.collected is True


@pytest.mark.asyncio
async def test_refund_x_cashu_sat_unit() -> None:
    x_cashu_token = "cashuAsat_token"
    in_tx = _make_cashu_tx(token=x_cashu_token, amount=0, unit="sat", type="in", request_id="req-sat")
    out_tx = _make_cashu_tx(token="cashuArefund_sat", amount=500, unit="sat", type="out", request_id="req-sat")

    session = MagicMock()
    session.exec = AsyncMock(side_effect=[_exec_result(in_tx), _exec_result(out_tx)])
    session.add = MagicMock()
    session.commit = AsyncMock()

    result = await refund_wallet_endpoint(
        authorization="Bearer sk-somekey",
        x_cashu=x_cashu_token,
        session=session,
    )

    assert isinstance(result, JSONResponse)
    body = json.loads(result.body)
    assert body["token"] == "cashuArefund_sat"
    assert body["sats"] == "500"
    assert "msats" not in body
    assert result.headers["X-Cashu"] == "cashuArefund_sat"


@pytest.mark.asyncio
async def test_refund_x_cashu_not_found_raises_404() -> None:
    from fastapi import HTTPException

    session = MagicMock()
    session.exec = AsyncMock(return_value=_exec_result(None))

    with pytest.raises(HTTPException) as exc_info:
        await refund_wallet_endpoint(
            authorization="Bearer sk-somekey",
            x_cashu="cashuAmissing_token",
            session=session,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_refund_x_cashu_swept_raises_410() -> None:
    from fastapi import HTTPException

    in_tx = _make_cashu_tx(token="cashuAswept_token", amount=0, unit="msat", type="in", request_id="req-swept")
    out_tx = _make_cashu_tx(token="cashuAswept", amount=100, unit="msat", type="out", request_id="req-swept", swept=True)

    session = MagicMock()
    session.exec = AsyncMock(side_effect=[_exec_result(in_tx), _exec_result(out_tx)])

    with pytest.raises(HTTPException) as exc_info:
        await refund_wallet_endpoint(
            authorization="Bearer sk-somekey",
            x_cashu="cashuAswept_token",
            session=session,
        )

    assert exc_info.value.status_code == 410


# ---------------------------------------------------------------------------
# source field defaults
# ---------------------------------------------------------------------------


def test_cashu_transaction_source_defaults_to_x_cashu() -> None:
    tx = CashuTransaction(token="cashuAtest", amount=100, unit="msat")
    assert tx.source == "x-cashu"


def test_cashu_transaction_source_can_be_apikey() -> None:
    tx = CashuTransaction(token="cashuAtest", amount=100, unit="msat", source="apikey")
    assert tx.source == "apikey"


# ---------------------------------------------------------------------------
# apikey-based refund: token logging and CashuTransaction storage
# ---------------------------------------------------------------------------


def _make_api_key(
    balance: int = 5000,
    refund_currency: str | None = "sat",
    refund_mint_url: str | None = "https://mint.example.com",
    refund_address: str | None = None,
    parent_key_hash: str | None = None,
) -> ApiKey:
    key = ApiKey(hashed_key="testhash")
    key.balance = balance
    key.reserved_balance = 0
    key.refund_currency = refund_currency
    key.refund_mint_url = refund_mint_url
    key.refund_address = refund_address
    key.parent_key_hash = parent_key_hash
    key.total_spent = 0
    key.total_requests = 0
    return key


@pytest.mark.asyncio
async def test_apikey_refund_stores_cashu_transaction_with_apikey_source() -> None:
    key = _make_api_key(balance=5000, refund_currency="sat")
    refund_token = "cashuArefund_apikey_token"

    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    with (
        patch("routstr.balance.validate_bearer_key", AsyncMock(return_value=key)),
        patch("routstr.balance.get_billing_key", AsyncMock(return_value=key)),
        patch("routstr.balance.send_token", AsyncMock(return_value=refund_token)),
        patch("routstr.balance.store_cashu_transaction", AsyncMock()) as mock_store,
        patch("routstr.balance._refund_cache_get", AsyncMock(return_value=None)),
        patch("routstr.balance._refund_cache_set", AsyncMock()),
    ):
        result = await refund_wallet_endpoint(
            authorization="Bearer sk-testhash",
            x_cashu=None,
            session=session,
        )

    assert isinstance(result, dict)
    assert result["token"] == refund_token

    mock_store.assert_awaited_once()
    call_kwargs = mock_store.call_args.kwargs
    assert call_kwargs["source"] == "apikey"
    assert call_kwargs["token"] == refund_token
    assert call_kwargs["typ"] == "out"


@pytest.mark.asyncio
async def test_apikey_refund_logs_token() -> None:
    key = _make_api_key(balance=5000, refund_currency="sat")
    refund_token = "cashuAlogged_token"

    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    with (
        patch("routstr.balance.validate_bearer_key", AsyncMock(return_value=key)),
        patch("routstr.balance.get_billing_key", AsyncMock(return_value=key)),
        patch("routstr.balance.send_token", AsyncMock(return_value=refund_token)),
        patch("routstr.balance.store_cashu_transaction", AsyncMock()),
        patch("routstr.balance._refund_cache_get", AsyncMock(return_value=None)),
        patch("routstr.balance._refund_cache_set", AsyncMock()),
        patch("routstr.balance.logger") as mock_logger,
    ):
        await refund_wallet_endpoint(
            authorization="Bearer sk-testhash",
            x_cashu=None,
            session=session,
        )

    calls = [str(c) for c in mock_logger.info.call_args_list]
    assert any("cashu token issued" in c for c in calls)


@pytest.mark.asyncio
async def test_apikey_refund_log_includes_path() -> None:
    key = _make_api_key(balance=5000, refund_currency="sat")
    refund_token = "cashuApath_token"

    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    with (
        patch("routstr.balance.validate_bearer_key", AsyncMock(return_value=key)),
        patch("routstr.balance.get_billing_key", AsyncMock(return_value=key)),
        patch("routstr.balance.send_token", AsyncMock(return_value=refund_token)),
        patch("routstr.balance.store_cashu_transaction", AsyncMock()),
        patch("routstr.balance._refund_cache_get", AsyncMock(return_value=None)),
        patch("routstr.balance._refund_cache_set", AsyncMock()),
        patch("routstr.balance.logger") as mock_logger,
    ):
        await refund_wallet_endpoint(
            authorization="Bearer sk-testhash",
            x_cashu=None,
            session=session,
        )

    # Find the "cashu token issued" call and verify extra contains the path
    token_issued_calls = [
        c for c in mock_logger.info.call_args_list
        if c.args and "cashu token issued" in c.args[0]
    ]
    assert len(token_issued_calls) == 1
    extra = token_issued_calls[0].kwargs.get("extra", {})
    assert extra.get("path") == "/v1/wallet/refund"
