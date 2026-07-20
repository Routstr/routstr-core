from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from routstr.core import admin


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_mint", [None, "https://secondary.example"])
async def test_withdraw_uses_effective_mint_and_records_outgoing_transaction(
    monkeypatch: pytest.MonkeyPatch, requested_mint: str | None
) -> None:
    primary_mint = "https://primary.example"
    effective_mint = requested_mint or primary_mint
    wallet = object()
    proofs = [SimpleNamespace(amount=40), SimpleNamespace(amount=60)]
    token = "cashuBoutgoing"

    get_wallet = AsyncMock(return_value=wallet)
    get_proofs = Mock(return_value=proofs)
    filter_proofs = AsyncMock(return_value=proofs)
    send_token = AsyncMock(return_value=token)
    store_transaction = AsyncMock(return_value=True)

    monkeypatch.setattr(admin, "get_wallet", get_wallet)
    monkeypatch.setattr(admin, "get_proofs_per_mint_and_unit", get_proofs)
    monkeypatch.setattr(admin, "slow_filter_spend_proofs", filter_proofs)
    monkeypatch.setattr(admin, "send_token", send_token)
    monkeypatch.setattr(admin, "store_cashu_transaction", store_transaction)
    monkeypatch.setattr(admin.settings, "primary_mint", primary_mint)

    result = await admin.withdraw(
        Mock(),
        admin.WithdrawRequest(amount=75, mint_url=requested_mint, unit="sat"),
    )

    assert result == {"token": token}
    get_wallet.assert_awaited_once_with(effective_mint, "sat")
    get_proofs.assert_called_once_with(wallet, effective_mint, "sat", not_reserved=True)
    filter_proofs.assert_awaited_once_with(proofs, wallet)
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
async def test_withdraw_returns_issued_token_when_audit_storage_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mint = "https://primary.example"
    proofs = [SimpleNamespace(amount=100)]
    token = "cashuBrecoverable"

    monkeypatch.setattr(admin, "get_wallet", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        admin, "get_proofs_per_mint_and_unit", Mock(return_value=proofs)
    )
    monkeypatch.setattr(
        admin, "slow_filter_spend_proofs", AsyncMock(return_value=proofs)
    )
    monkeypatch.setattr(admin, "send_token", AsyncMock(return_value=token))
    monkeypatch.setattr(
        admin,
        "store_cashu_transaction",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    critical = Mock()
    monkeypatch.setattr(admin.logger, "critical", critical)
    monkeypatch.setattr(admin.settings, "primary_mint", mint)

    result = await admin.withdraw(Mock(), admin.WithdrawRequest(amount=75))

    assert result == {"token": token}
    critical.assert_called_once()
