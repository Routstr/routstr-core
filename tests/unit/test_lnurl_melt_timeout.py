"""LNURL melt attempts must not misclassify ambiguous payment outcomes."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cashu.core.base import MeltQuoteState

from routstr.core.settings import settings
from routstr.payment.lnurl import LNURLError, raw_send_to_lnurl

LNURL_DATA = {
    "callback_url": "https://ln.tld/cb",
    "min_sendable": 1_000,
    "max_sendable": 100_000_000,
}


def _wallet() -> tuple[MagicMock, list[MagicMock]]:
    proofs = [MagicMock(amount=1000)]
    wallet = MagicMock(url="https://mint.test")
    wallet.melt_quote = AsyncMock(return_value=MagicMock(fee_reserve=1, quote="q"))
    wallet.select_to_send = AsyncMock(return_value=(proofs, None))
    return wallet, proofs


def _lnurl_patches() -> tuple[Any, Any]:
    return (
        patch(
            "routstr.payment.lnurl.get_lnurl_data",
            AsyncMock(return_value=LNURL_DATA),
        ),
        patch(
            "routstr.payment.lnurl.get_lnurl_invoice",
            AsyncMock(return_value=("lnbc1...", {})),
        ),
    )


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_timeout_keeps_unpaid_outcome_ambiguous() -> None:
    wallet, proofs = _wallet()

    async def _hang(**kwargs: object) -> None:
        await asyncio.sleep(5)

    wallet.melt = AsyncMock(side_effect=_hang)
    wallet.get_melt_quote = AsyncMock(
        return_value=MagicMock(state=MeltQuoteState.unpaid)
    )
    data_patch, invoice_patch = _lnurl_patches()

    with (
        patch.object(settings, "mint_operation_timeout_seconds", 0.05),
        patch.object(settings, "mint_retry_max_attempts", 0),
        data_patch,
        invoice_patch,
        pytest.raises(LNURLError, match="outcome is ambiguous"),
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.get_melt_quote.assert_awaited_once_with("q")
    wallet.set_reserved_for_melt.assert_not_called()


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_timeout_reconciled_paid_is_success() -> None:
    wallet, proofs = _wallet()

    async def _hang(**kwargs: object) -> None:
        await asyncio.sleep(5)

    wallet.melt = AsyncMock(side_effect=_hang)
    wallet.get_melt_quote = AsyncMock(
        return_value=MagicMock(state=MeltQuoteState.paid)
    )
    data_patch, invoice_patch = _lnurl_patches()

    with (
        patch.object(settings, "mint_operation_timeout_seconds", 0.05),
        patch.object(settings, "mint_retry_max_attempts", 0),
        data_patch,
        invoice_patch,
    ):
        paid = await raw_send_to_lnurl(
            wallet, proofs, "owner@ln.tld", "sat", amount=1000
        )

    assert paid > 0
    wallet.get_melt_quote.assert_awaited_once_with("q")


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_pending_response_stays_ambiguous() -> None:
    wallet, proofs = _wallet()
    wallet.melt = AsyncMock(return_value=MagicMock(state=MeltQuoteState.pending))
    wallet.get_melt_quote = AsyncMock(
        return_value=MagicMock(state=MeltQuoteState.pending)
    )
    data_patch, invoice_patch = _lnurl_patches()

    with (
        patch.object(settings, "mint_operation_timeout_seconds", 5),
        data_patch,
        invoice_patch,
        pytest.raises(LNURLError, match="outcome is ambiguous"),
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.get_melt_quote.assert_awaited_once_with("q")


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_succeeds_on_explicit_paid_response() -> None:
    wallet, proofs = _wallet()
    wallet.melt = AsyncMock(return_value=MagicMock(state=MeltQuoteState.paid))
    wallet.get_melt_quote = AsyncMock()
    data_patch, invoice_patch = _lnurl_patches()

    with (
        patch.object(settings, "mint_operation_timeout_seconds", 5),
        data_patch,
        invoice_patch,
    ):
        paid = await raw_send_to_lnurl(
            wallet, proofs, "owner@ln.tld", "sat", amount=1000
        )

    assert paid > 0
    wallet.melt.assert_awaited_once()
    wallet.get_melt_quote.assert_not_awaited()
