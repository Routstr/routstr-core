"""Timeout reconciliation for LNURL melts."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cashu.core.base import MeltQuoteState

from routstr.payment import lnurl
from routstr.payment.lnurl import (
    LNURLError,
    MeltOutcomeUncertainError,
    raw_send_to_lnurl,
)

PROOFS = [MagicMock(amount=1000)]
LNURL_DATA = {
    "callback_url": "https://ln.tld/cb",
    "min_sendable": 1_000,
    "max_sendable": 100_000_000,
}


def _wallet(state: MeltQuoteState | None) -> MagicMock:
    wallet = MagicMock()
    wallet.melt_quote = AsyncMock(return_value=MagicMock(fee_reserve=1, quote="q"))
    wallet.select_to_send = AsyncMock(return_value=(PROOFS, None))

    async def _hang(**kwargs: object) -> None:
        await asyncio.sleep(5)

    wallet.melt = AsyncMock(side_effect=_hang)
    wallet.get_melt_quote = AsyncMock(
        return_value=None if state is None else MagicMock(state=state)
    )
    wallet.invalidate = AsyncMock()
    wallet.set_reserved_for_melt = AsyncMock()
    wallet.load_proofs = AsyncMock()
    return wallet


async def _send(wallet: MagicMock) -> int:
    with patch.object(lnurl, "MELT_TIMEOUT_SECONDS", 0.01), patch.object(
        lnurl, "MELT_RECONCILE_TIMEOUT_SECONDS", 0.01
    ), patch(
        "routstr.payment.lnurl.get_lnurl_data", AsyncMock(return_value=LNURL_DATA)
    ), patch(
        "routstr.payment.lnurl.get_lnurl_invoice",
        AsyncMock(return_value=("lnbc1...", {})),
    ):
        return await raw_send_to_lnurl(
            wallet, PROOFS, "owner@ln.tld", "sat", amount=1000
        )


@pytest.mark.asyncio
async def test_timeout_paid_quote_invalidates_proofs_and_reloads() -> None:
    wallet = _wallet(MeltQuoteState.paid)

    paid = await _send(wallet)

    assert paid > 0
    wallet.get_melt_quote.assert_awaited_once_with("q")
    wallet.invalidate.assert_awaited_once_with(PROOFS)
    wallet.load_proofs.assert_awaited_once_with(reload=True)
    wallet.set_reserved_for_melt.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_unpaid_quote_releases_proofs_and_reloads() -> None:
    wallet = _wallet(MeltQuoteState.unpaid)

    with pytest.raises(LNURLError, match="quote q is unpaid"):
        await _send(wallet)

    wallet.set_reserved_for_melt.assert_awaited_once_with(
        PROOFS, reserved=False, quote_id=None
    )
    wallet.load_proofs.assert_awaited_once_with(reload=True)
    wallet.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_pending_quote_preserves_reservation() -> None:
    wallet = _wallet(MeltQuoteState.pending)

    with pytest.raises(MeltOutcomeUncertainError, match="mint reports PENDING"):
        await _send(wallet)

    wallet.invalidate.assert_not_awaited()
    wallet.set_reserved_for_melt.assert_not_awaited()
    wallet.load_proofs.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quote_result", "match"),
    [
        (None, "mint returned no quote"),
        (RuntimeError("mint unavailable"), "quote reconciliation failed"),
    ],
)
async def test_reconciliation_failure_preserves_reservation(
    quote_result: Any, match: str
) -> None:
    wallet = _wallet(None)
    if isinstance(quote_result, Exception):
        wallet.get_melt_quote.side_effect = quote_result
    else:
        wallet.get_melt_quote.return_value = quote_result

    with pytest.raises(MeltOutcomeUncertainError, match=match):
        await _send(wallet)

    wallet.invalidate.assert_not_awaited()
    wallet.set_reserved_for_melt.assert_not_awaited()
    wallet.load_proofs.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciliation_query_has_its_own_timeout() -> None:
    wallet = _wallet(None)

    async def _hang_quote(quote_id: str) -> None:
        await asyncio.sleep(5)

    wallet.get_melt_quote.side_effect = _hang_quote

    with pytest.raises(MeltOutcomeUncertainError, match="quote reconciliation failed"):
        await _send(wallet)

    wallet.invalidate.assert_not_awaited()
    wallet.set_reserved_for_melt.assert_not_awaited()
    wallet.load_proofs.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_succeeds_within_timeout() -> None:
    wallet = _wallet(None)
    wallet.melt = AsyncMock(return_value=MagicMock())

    with patch.object(lnurl, "MELT_TIMEOUT_SECONDS", 5), patch(
        "routstr.payment.lnurl.get_lnurl_data", AsyncMock(return_value=LNURL_DATA)
    ), patch(
        "routstr.payment.lnurl.get_lnurl_invoice",
        AsyncMock(return_value=("lnbc1...", {})),
    ):
        paid = await raw_send_to_lnurl(
            wallet, PROOFS, "owner@ln.tld", "sat", amount=1000
        )

    assert paid > 0
    wallet.melt.assert_awaited_once()
    wallet.get_melt_quote.assert_not_awaited()
