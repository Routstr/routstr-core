from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest

from routstr.core.settings import settings
from routstr.wallet import _payout_mint_and_unit


@asynccontextmanager
async def session() -> AsyncIterator[Mock]:
    yield Mock()


@pytest.mark.asyncio
@pytest.mark.parametrize("unit,scale", [("sat", 1), ("msat", 1000)])
@pytest.mark.parametrize(
    "balance,liability,expected",
    [(1000, 0, 100), (80, 30000, 50), (20, 20000, None), (0, 0, None), (10, 0, None)],
)
async def test_payout_limits_and_proof_refresh(
    unit: str, scale: int, balance: int, liability: int, expected: int | None
) -> None:
    send = AsyncMock()
    get_wallet = AsyncMock()
    check = AsyncMock(side_effect=lambda ps, w: ps)
    sleep = AsyncMock()
    with (
        patch.object(settings, "min_payout_sat", 10),
        patch.object(settings, "max_payout_sat", 100),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            return_value=[Mock(amount=balance * scale)],
        ),
        patch("routstr.wallet.slow_filter_spend_proofs", check),
        patch("routstr.wallet.db.create_session", session),
        patch(
            "routstr.wallet.db.total_user_liability", AsyncMock(return_value=liability)
        ),
        patch("routstr.wallet.asyncio.sleep", sleep),
        patch("routstr.wallet.raw_send_to_lnurl", send),
    ):
        await _payout_mint_and_unit("https://mint.test", unit)
    get_wallet.assert_awaited_once_with(
        "https://mint.test", unit, force_reload_proofs=True
    )
    if expected is None:
        send.assert_not_awaited()
    else:
        assert send.await_args is not None
        assert send.await_args.kwargs["amount"] == expected * scale
    if balance <= 10:
        check.assert_not_awaited()
        sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_proof_check_never_pays_partial_balance() -> None:
    send = AsyncMock()
    with (
        patch("routstr.wallet.get_wallet", AsyncMock()),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            return_value=[Mock(amount=1_000_000)],
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=ValueError("Invalid proof-state response")),
        ),
        patch("routstr.wallet.raw_send_to_lnurl", send),
    ):
        await _payout_mint_and_unit("https://mint.test", "sat")
    send.assert_not_awaited()
