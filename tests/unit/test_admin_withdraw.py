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

    monkeypatch.setattr(admin, "send_token_from_owner_locked", send_token)
    monkeypatch.setattr(admin, "token_mint_url", Mock(return_value=effective_mint))
    monkeypatch.setattr(admin, "store_cashu_transaction", store_transaction)
    monkeypatch.setattr(admin.settings, "primary_mint", primary_mint)

    result = await admin.withdraw(
        Mock(),
        admin.WithdrawRequest(amount=75, mint_url=requested_mint, unit="sat"),
    )

    assert result == {"token": token, "mint_url": effective_mint}
    send_token.assert_awaited_once_with(
        75, "sat", effective_mint, fallback_on_owner_shortfall=True
    )
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

    monkeypatch.setattr(
        admin, "send_token_from_owner_locked", AsyncMock(return_value=token)
    )
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
async def test_withdraw_falls_back_past_customer_backed_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_mint = "https://primary.example"
    actual_mint = "https://secondary.example"
    proofs = [SimpleNamespace(amount=75, reserved=False, id="00")]
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
    token = (
        "cashuA" + base64.urlsafe_b64encode(json.dumps(token_payload).encode()).decode()
    )
    wallet = SimpleNamespace(
        active_proofs=Mock(return_value=proofs),
        split=AsyncMock(),
        serialize_proofs=AsyncMock(return_value=token),
        set_reserved_for_send=AsyncMock(),
    )
    customer_wallet = Mock(serialize_proofs=AsyncMock())
    get_wallet = AsyncMock(side_effect=[customer_wallet, wallet])
    store_transaction = AsyncMock(return_value=True)

    monkeypatch.setattr(admin.settings, "primary_mint", requested_mint)
    monkeypatch.setattr(admin.settings, "cashu_mints", [requested_mint, actual_mint])
    monkeypatch.setattr(wallet_module, "get_wallet", get_wallet)
    monkeypatch.setattr(
        wallet_module, "get_proofs_per_mint_and_unit", Mock(return_value=proofs)
    )
    monkeypatch.setattr(
        wallet_module, "slow_filter_spend_proofs", AsyncMock(return_value=proofs)
    )
    monkeypatch.setattr(
        wallet_module,
        "_owner_balance_for_mint_and_unit",
        AsyncMock(side_effect=[0, 75]),
    )
    monkeypatch.setattr(admin, "store_cashu_transaction", store_transaction)

    result = await admin.withdraw(
        Mock(), admin.WithdrawRequest(amount=75, mint_url=requested_mint)
    )

    assert result == {"token": token, "mint_url": actual_mint}
    assert [call.args[0] for call in get_wallet.await_args_list] == [
        requested_mint,
        actual_mint,
    ]
    assert all(call.kwargs["force_reload"] for call in get_wallet.await_args_list)
    customer_wallet.serialize_proofs.assert_not_awaited()
    wallet.split.assert_not_awaited()
    wallet.set_reserved_for_send.assert_awaited_once_with(proofs, reserved=True)
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
async def test_withdraw_maps_insufficient_owner_funds_to_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send = AsyncMock(
        side_effect=wallet_module.InsufficientOwnerCashuBalance(
            "Insufficient withdrawable owner balance"
        )
    )
    monkeypatch.setattr(admin, "send_token_from_owner_locked", send)

    with pytest.raises(HTTPException) as exc_info:
        await admin.withdraw(Mock(), admin.WithdrawRequest(amount=75))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Insufficient withdrawable owner balance"


@pytest.mark.asyncio
async def test_withdraw_reports_too_many_small_coins_as_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "Too many small coins: selected proofs cover 86648 sat after swap fees"
    monkeypatch.setattr(
        admin,
        "send_token_from_owner_locked",
        AsyncMock(side_effect=ValueError(message)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await admin.withdraw(Mock(), admin.WithdrawRequest(amount=87628))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == message
