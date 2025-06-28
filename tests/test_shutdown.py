import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.main import app, lifespan
from tests.conftest import TEST_ENV


@pytest.mark.asyncio
async def test_background_tasks_cancel_on_shutdown() -> None:
    pricing_started = asyncio.Event()
    pricing_cancelled = asyncio.Event()

    async def fake_update() -> None:
        pricing_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pricing_cancelled.set()
            raise

    refund_started = asyncio.Event()
    refund_cancelled = asyncio.Event()

    async def fake_refund() -> None:
        refund_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            refund_cancelled.set()
            raise

    with patch.dict("os.environ", TEST_ENV, clear=True):
        mock_wallet = AsyncMock()
        mock_wallet.__aenter__ = AsyncMock(return_value=mock_wallet)
        mock_wallet.__aexit__ = AsyncMock(return_value=None)
        mock_state = MagicMock()
        mock_state.balance = 1000
        mock_wallet.fetch_wallet_state = AsyncMock(return_value=mock_state)
        mock_wallet.send_to_lnurl = AsyncMock(return_value=100)
        mock_wallet.redeem = AsyncMock(return_value=(1, "sat"))
        mock_wallet.send = AsyncMock(return_value="cashuAtoken123")

        with (
            patch("router.cashu.Wallet.create", AsyncMock(return_value=mock_wallet)),
            patch("router.cashu.WALLET", mock_wallet),
        ):
            with (
                patch("router.main.update_sats_pricing", new=fake_update),
                patch("router.main.check_for_refunds", new=fake_refund),
            ):
                async with lifespan(app):
                    await pricing_started.wait()
                    await refund_started.wait()

    assert pricing_cancelled.is_set()
    assert refund_cancelled.is_set()
