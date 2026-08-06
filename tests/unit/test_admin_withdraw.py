import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

import routstr.wallet as wallet_module
from routstr.core import admin


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_mint", [None, "https://secondary.example"])
async def test_withdraw_uses_effective_mint_and_records_outgoing_transaction(
    monkeypatch: pytest.MonkeyPatch, requested_mint: str | None
) -> None:
    primary_mint = "https://primary.example"
    effective_mint = requested_mint or primary_mint
    token = "cashuBoutgoing"
    send_token = AsyncMock(return_value=token)
    store_transaction = AsyncMock(return_value=True)

    monkeypatch.setattr(admin, "send_token", send_token)
    monkeypatch.setattr(admin, "token_mint_url", Mock(return_value=effective_mint))
    monkeypatch.setattr(admin, "store_cashu_transaction", store_transaction)
    monkeypatch.setattr(admin.settings, "primary_mint", primary_mint)

    result = await admin.withdraw(
        Mock(),
        admin.WithdrawRequest(amount=75, mint_url=requested_mint, unit="sat"),
    )

    assert result == {"token": token, "mint_url": effective_mint}
    send_token.assert_awaited_once_with(75, "sat", effective_mint)
    store_transaction.assert_awaited_once_with(
        token=token,
        amount=75,
        unit="sat",
        mint_url=effective_mint,
        typ="out",
        collected=False,
        source="admin",
    )


@pytest.mark.asyncio
async def test_withdraw_propagates_audit_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mint = "https://primary.example"
    token = "cashuBrecoverable"

    monkeypatch.setattr(admin, "send_token", AsyncMock(return_value=token))
    monkeypatch.setattr(admin, "token_mint_url", Mock(return_value=mint))
    monkeypatch.setattr(
        admin,
        "store_cashu_transaction",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(admin.settings, "primary_mint", mint)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await admin.withdraw(Mock(), admin.WithdrawRequest(amount=75))


@pytest.mark.asyncio
async def test_withdraw_falls_back_from_insufficient_preferred_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_mint = "https://primary.example"
    actual_mint = "https://secondary.example"
    proofs = [SimpleNamespace(amount=100, reserved=False, id="00")]
    token_payload = {
        "token": [
            {
                "mint": actual_mint,
                "proofs": [
                    {
                        "id": "00",
                        "amount": 75,
                        "secret": "secret",
                        "C": "02" + "00" * 32,
                    }
                ],
            }
        ],
        "unit": "sat",
    }
    token = "cashuA" + base64.urlsafe_b64encode(
        json.dumps(token_payload).encode()
    ).decode()
    wallet = SimpleNamespace(
        keysets={},
        proofs=proofs,
        select_to_send=AsyncMock(return_value=(proofs, 0)),
        serialize_proofs=AsyncMock(return_value=token),
        set_reserved_for_send=AsyncMock(),
    )
    find_funded = AsyncMock(return_value=actual_mint)
    store_transaction = AsyncMock(return_value=True)

    monkeypatch.setattr(wallet_module, "find_trusted_mint_with_funds", find_funded)
    monkeypatch.setattr(wallet_module, "get_wallet", AsyncMock(return_value=wallet))
    monkeypatch.setattr(
        wallet_module, "get_proofs_per_mint_and_unit", Mock(return_value=proofs)
    )
    monkeypatch.setattr(admin, "store_cashu_transaction", store_transaction)

    result = await admin.withdraw(
        Mock(), admin.WithdrawRequest(amount=75, mint_url=requested_mint)
    )

    assert result == {"token": token, "mint_url": actual_mint}
    find_funded.assert_awaited_once_with(
        75, "sat", requested_mint, force_reload=True
    )
    wallet.select_to_send.assert_awaited_once()
    store_transaction.assert_awaited_once_with(
        token=token,
        amount=75,
        unit="sat",
        mint_url=actual_mint,
        typ="out",
        collected=False,
        source="admin",
    )


@pytest.mark.asyncio
async def test_withdraw_maps_true_aggregate_insufficient_funds_to_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        admin,
        "send_token",
        AsyncMock(
            side_effect=ValueError(
                "No trusted mint has 75 sat available; balances={'mint': 0}"
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await admin.withdraw(Mock(), admin.WithdrawRequest(amount=75))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Insufficient wallet balance"
