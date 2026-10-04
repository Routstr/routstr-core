"""Integration coverage for automatic foreign-mint wallet top-ups."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from routstr.core.settings import settings

PRIMARY_MINT = "https://primary.example"
FOREIGN_MINT = "https://foreign.example"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_with_foreign_mint_token_swaps_automatically(
    authenticated_client: AsyncClient,
) -> None:
    swap = AsyncMock(return_value=997_000)

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
    assert response.json() == {"msats": 997_000}
    swap.assert_awaited_once()
    assert swap.await_args is not None
    assert swap.await_args.args[0] == "cashuAtest_foreign_token"
