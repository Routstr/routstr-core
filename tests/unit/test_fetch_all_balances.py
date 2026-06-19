from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.wallet import fetch_all_balances


@asynccontextmanager
async def _fake_session():  # type: ignore[no-untyped-def]
    yield MagicMock()


def _patches(proof_amount: int = 1000):  # type: ignore[no-untyped-def]
    proof = MagicMock(amount=proof_amount)
    return [
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[proof]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch(
            "routstr.wallet.db.balances_for_mint_and_unit",
            AsyncMock(return_value=0),
        ),
        patch("routstr.wallet.db.create_session", _fake_session),
    ]


@pytest.mark.asyncio
async def test_fetch_all_balances_falls_back_to_primary_mint() -> None:
    """With empty cashu_mints, balances are still fetched for primary_mint."""
    from routstr.core.settings import settings

    with patch.object(settings, "cashu_mints", []), patch.object(
        settings, "primary_mint", "http://primary:3338"
    ):
        for p in _patches(proof_amount=1000):
            p.start()
        try:
            details, total_wallet, total_user, owner = await fetch_all_balances(
                units=["sat"]
            )
        finally:
            patch.stopall()

    assert [d["mint_url"] for d in details] == ["http://primary:3338"]
    assert total_wallet == 1000


@pytest.mark.asyncio
async def test_fetch_all_balances_no_duplicate_primary_mint() -> None:
    """primary_mint already in cashu_mints is not inspected twice."""
    from routstr.core.settings import settings

    with patch.object(
        settings, "cashu_mints", ["http://primary:3338"]
    ), patch.object(settings, "primary_mint", "http://primary:3338"):
        for p in _patches(proof_amount=1000):
            p.start()
        try:
            details, total_wallet, _total_user, _owner = await fetch_all_balances(
                units=["sat"]
            )
        finally:
            patch.stopall()

    assert [d["mint_url"] for d in details] == ["http://primary:3338"]
    assert total_wallet == 1000
