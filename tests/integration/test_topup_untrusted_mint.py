"""
Integration test for the wallet topup endpoint with a foreign-mint token.

Tokens are only accepted from trusted mints (primary_mint plus cashu_mints)
and are always redeemed on the mint that issued them. A token from any other
mint is rejected offline, before any network contact with that mint, with a
dedicated error type and code. This replaces the former cross-mint swap
path, so there is no fee-retry behaviour left to exercise here.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import AsyncClient

from routstr.core.settings import settings

# Captured at collection time, before the integration_app fixture replaces it
# with the testmint stub (see conftest.py).
from routstr.wallet import recieve_token as _real_recieve_token

PRIMARY_MINT = "http://localhost:3338"
FOREIGN_MINT = "http://foreign-mint:3338"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_with_foreign_mint_token_is_rejected_without_mint_contact(
    authenticated_client: AsyncClient,
) -> None:
    mock_token = Mock()
    mock_token.mint = FOREIGN_MINT
    mock_token.unit = "sat"
    mock_token.amount = 1000
    mock_token.keysets = ["keyset"]
    get_wallet = AsyncMock()

    with (
        patch("routstr.wallet.recieve_token", _real_recieve_token),
        patch("routstr.wallet.deserialize_token_from_string", return_value=mock_token),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch.object(settings, "primary_mint", PRIMARY_MINT),
        patch.object(settings, "primary_mint_unit", "sat"),
        patch.object(settings, "cashu_mints", [PRIMARY_MINT]),
    ):
        response = await authenticated_client.post(
            "/v1/wallet/topup",
            params={"cashu_token": "cashuAtest_foreign_token"},
        )

    assert response.status_code == 400
    error = response.json()["detail"]["error"]
    assert error["type"] == "untrusted_mint"
    assert error["code"] == "cashu_untrusted_source_mint"
    assert FOREIGN_MINT not in error["message"]
    get_wallet.assert_not_awaited()
