import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from routstr.balance import refund_wallet_endpoint
from routstr.core.db import CashuTransaction


def _make_cashu_tx(token: str, amount: int, unit: str, swept: bool = False) -> CashuTransaction:
    tx = CashuTransaction(token=token, amount=amount, unit=unit)
    tx.swept = swept
    tx.collected = False
    return tx


@pytest.mark.asyncio
async def test_refund_x_cashu_returns_token() -> None:
    x_cashu_token = "cashuAtest_token_value"
    expected_hash = hashlib.sha256(x_cashu_token.strip().encode()).hexdigest()
    tx = _make_cashu_tx(token="cashuArefund_token", amount=1000, unit="msat")

    session = MagicMock()
    session.get = AsyncMock(return_value=tx)
    session.add = MagicMock()
    session.commit = AsyncMock()

    result = await refund_wallet_endpoint(
        authorization="Bearer sk-somekey",
        x_cashu=x_cashu_token,
        session=session,
    )

    session.get.assert_awaited_once_with(CashuTransaction, expected_hash)
    import json
    body = json.loads(result.body)
    assert body["token"] == "cashuArefund_token"
    assert body["msats"] == "1000"
    assert result.headers["X-Cashu"] == "cashuArefund_token"
    assert tx.collected is True


@pytest.mark.asyncio
async def test_refund_x_cashu_sat_unit() -> None:
    x_cashu_token = "cashuAsat_token"
    tx = _make_cashu_tx(token="cashuArefund_sat", amount=500, unit="sat")

    session = MagicMock()
    session.get = AsyncMock(return_value=tx)
    session.add = MagicMock()
    session.commit = AsyncMock()

    result = await refund_wallet_endpoint(
        authorization="Bearer sk-somekey",
        x_cashu=x_cashu_token,
        session=session,
    )

    import json
    body = json.loads(result.body)
    assert body["token"] == "cashuArefund_sat"
    assert body["sats"] == "500"
    assert "msats" not in body
    assert result.headers["X-Cashu"] == "cashuArefund_sat"


@pytest.mark.asyncio
async def test_refund_x_cashu_not_found_raises_404() -> None:
    from fastapi import HTTPException

    session = MagicMock()
    session.get = AsyncMock(return_value=None)

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

    tx = _make_cashu_tx(token="cashuAswept", amount=100, unit="msat", swept=True)

    session = MagicMock()
    session.get = AsyncMock(return_value=tx)

    with pytest.raises(HTTPException) as exc_info:
        await refund_wallet_endpoint(
            authorization="Bearer sk-somekey",
            x_cashu="cashuAswept_token",
            session=session,
        )

    assert exc_info.value.status_code == 410
