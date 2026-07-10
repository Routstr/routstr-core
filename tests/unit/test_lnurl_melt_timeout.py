"""raw_send_to_lnurl() must not hang forever on an unresponsive mint.

The Cashu library issues POST /v1/melt/bolt11 with timeout=None, so a hung
mint would block the melt (and the payout loop) indefinitely. raw_send_to_lnurl
now wraps wallet.melt() in asyncio.wait_for(MELT_TIMEOUT_SECONDS) and surfaces a
timeout as LNURLError instead of hanging.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.payment import lnurl
from routstr.payment.lnurl import LNURLError, raw_send_to_lnurl


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_times_out_on_hung_melt() -> None:
    proofs = [MagicMock(amount=1000)]

    wallet = MagicMock()
    wallet.melt_quote = AsyncMock(return_value=MagicMock(fee_reserve=1, quote="q"))
    wallet.select_to_send = AsyncMock(return_value=(proofs, None))

    async def _hang(**kwargs: object) -> None:
        await asyncio.sleep(5)  # far longer than the patched timeout

    wallet.melt = AsyncMock(side_effect=_hang)

    lnurl_data = {
        "callback_url": "https://ln.tld/cb",
        "min_sendable": 1_000,
        "max_sendable": 100_000_000,
    }

    with patch.object(lnurl, "MELT_TIMEOUT_SECONDS", 0.05), patch(
        "routstr.payment.lnurl.get_lnurl_data", AsyncMock(return_value=lnurl_data)
    ), patch(
        "routstr.payment.lnurl.get_lnurl_invoice",
        AsyncMock(return_value=("lnbc1...", {})),
    ):
        with pytest.raises(LNURLError, match="Melt timed out"):
            await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_succeeds_within_timeout() -> None:
    """A prompt melt still returns the net amount, unaffected by the guard."""
    proofs = [MagicMock(amount=1000)]

    wallet = MagicMock()
    wallet.melt_quote = AsyncMock(return_value=MagicMock(fee_reserve=1, quote="q"))
    wallet.select_to_send = AsyncMock(return_value=(proofs, None))
    wallet.melt = AsyncMock(return_value=MagicMock())

    lnurl_data = {
        "callback_url": "https://ln.tld/cb",
        "min_sendable": 1_000,
        "max_sendable": 100_000_000,
    }

    with patch.object(lnurl, "MELT_TIMEOUT_SECONDS", 5), patch(
        "routstr.payment.lnurl.get_lnurl_data", AsyncMock(return_value=lnurl_data)
    ), patch(
        "routstr.payment.lnurl.get_lnurl_invoice",
        AsyncMock(return_value=("lnbc1...", {})),
    ):
        paid = await raw_send_to_lnurl(
            wallet, proofs, "owner@ln.tld", "sat", amount=1000
        )

    assert paid > 0
    wallet.melt.assert_awaited_once()
