"""Integration coverage for automatic foreign-mint wallet top-ups."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from routstr.core.settings import settings
from routstr.foreign_mint_swap import SwapInResult
from routstr.wallet import SWAP_BUSY_RETRY_AFTER_SECONDS, ForeignMintBusyError

PRIMARY_MINT = "https://primary.example"
FOREIGN_MINT = "https://foreign.example"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_with_foreign_mint_token_swaps_automatically(
    authenticated_client: AsyncClient,
) -> None:
    swap = AsyncMock(
        return_value=SwapInResult(
            credited_msats=997_000,
            change_token="cashuAchange",
            change_amount=3,
            change_unit="sat",
        )
    )

    with (
        patch("routstr.balance.token_mint_url", return_value=FOREIGN_MINT),
        patch("routstr.balance.swap_in_and_credit", swap),
        patch.object(settings, "primary_mint", PRIMARY_MINT),
        patch.object(settings, "primary_mint_unit", "sat"),
        patch.object(settings, "cashu_mints", [PRIMARY_MINT]),
    ):
        response = await authenticated_client.post(
            "/v1/wallet/topup",
            params={"cashu_token": "cashuAtest_foreign_token"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "msats": 997_000,
        "change_token": "cashuAchange",
        "change_amount": 3,
        "change_unit": "sat",
    }
    swap.assert_awaited_once()
    assert swap.await_args is not None
    assert swap.await_args.args[0] == "cashuAtest_foreign_token"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_while_mint_busy_returns_retryable_error(
    authenticated_client: AsyncClient,
) -> None:
    swap = AsyncMock(side_effect=ForeignMintBusyError("swap in progress"))

    with (
        patch("routstr.balance.token_mint_url", return_value=FOREIGN_MINT),
        patch("routstr.balance.swap_in_and_credit", swap),
        patch.object(settings, "primary_mint", PRIMARY_MINT),
        patch.object(settings, "cashu_mints", [PRIMARY_MINT]),
    ):
        response = await authenticated_client.post(
            "/v1/wallet/topup",
            params={"cashu_token": "cashuAtest_foreign_token"},
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(SWAP_BUSY_RETRY_AFTER_SECONDS)
    error = response.json()["detail"]["error"]
    assert (error["type"], error["code"]) == ("swap_busy", "cashu_swap_busy")
