import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import JSONResponse

from routstr.balance import refund_wallet_endpoint
from routstr.core.db import CashuTransaction


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
