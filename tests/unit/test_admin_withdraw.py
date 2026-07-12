from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.core.admin import WithdrawRequest, withdraw


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_mint", [None, "https://other.mint"])
async def test_withdraw_persists_outgoing_token_with_effective_mint(
    requested_mint: str | None,
) -> None:
    primary_mint = "https://primary.mint"
    effective_mint = requested_mint or primary_mint
    proof = MagicMock(amount=100)

    with (
        patch("routstr.core.admin.get_wallet", AsyncMock(return_value=MagicMock())) as get_wallet,
        patch("routstr.core.admin.get_proofs_per_mint_and_unit", return_value=[proof]) as get_proofs,
        patch("routstr.core.admin.slow_filter_spend_proofs", AsyncMock(return_value=[proof])),
        patch("routstr.core.admin.send_token", AsyncMock(return_value="cashu-token")) as send_token,
        patch("routstr.core.admin.store_cashu_transaction", AsyncMock(return_value=True)) as store,
        patch("routstr.core.settings.settings.primary_mint", primary_mint),
    ):
        result = await withdraw(MagicMock(), WithdrawRequest(amount=50, mint_url=requested_mint))

    assert result == {"token": "cashu-token"}
    get_wallet.assert_awaited_once_with(effective_mint, "sat")
    get_proofs.assert_called_once_with(get_wallet.return_value, effective_mint, "sat", not_reserved=True)
    send_token.assert_awaited_once_with(50, "sat", effective_mint)
    store.assert_awaited_once_with(
        token="cashu-token",
        amount=50,
        unit="sat",
        mint_url=effective_mint,
        typ="out",
        collected=False,
        source="admin",
    )
