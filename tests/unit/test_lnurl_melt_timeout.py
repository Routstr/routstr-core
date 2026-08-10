"""LNURL melt attempts must not misclassify ambiguous payment outcomes."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cashu.core.base import MeltQuoteState

from routstr.core.settings import settings
from routstr.mint import MintCooldownError, MintRateGuard
from routstr.payment.lnurl import (
    MeltOutcomeAmbiguousError,
    raw_send_to_lnurl,
)

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
        pytest.raises(MeltOutcomeAmbiguousError, match="outcome is ambiguous"),
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
    wallet.get_melt_quote = AsyncMock(return_value=MagicMock(state=MeltQuoteState.paid))
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
        pytest.raises(MeltOutcomeAmbiguousError, match="outcome is ambiguous"),
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.get_melt_quote.assert_awaited_once_with("q")


@pytest.mark.asyncio
@pytest.mark.parametrize("rate_error", ["cooldown", "http_429"])
async def test_raw_send_to_lnurl_rate_rejection_unreserves_proofs(
    rate_error: str,
) -> None:
    wallet, proofs = _wallet()
    wallet.melt = AsyncMock()
    wallet.set_reserved_for_send = AsyncMock()
    data_patch, invoice_patch = _lnurl_patches()

    async def run_operation(factory: Any, *, op_name: str, **_: object) -> Any:
        if op_name == "lnurl_melt":
            if rate_error == "cooldown":
                raise MintCooldownError(str(wallet.url), 60)
            request = httpx.Request("POST", f"{wallet.url}/v1/melt/bolt11")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError(
                "rate limited", request=request, response=response
            )
        return await factory()

    with (
        data_patch,
        invoice_patch,
        patch(
            "routstr.payment.lnurl.run_mint_operation",
            side_effect=run_operation,
        ),
        pytest.raises((MintCooldownError, httpx.HTTPStatusError)),
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.melt.assert_not_awaited()
    wallet.set_reserved_for_send.assert_awaited_once_with(proofs, reserved=False)


@pytest.mark.asyncio
async def test_real_mint_wrapper_http_429_unreserves_proofs() -> None:
    wallet, proofs = _wallet()
    request = httpx.Request("POST", f"{wallet.url}/v1/melt/bolt11")
    response = httpx.Response(429, request=request)
    wallet.melt = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "rate limited", request=request, response=response
        )
    )
    wallet.set_reserved_for_send = AsyncMock()
    data_patch, invoice_patch = _lnurl_patches()

    with (
        patch.object(settings, "mint_retry_max_attempts", 0),
        data_patch,
        invoice_patch,
        pytest.raises(httpx.HTTPStatusError),
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.melt.assert_awaited_once()
    wallet.set_reserved_for_send.assert_awaited_once_with(proofs, reserved=False)
    MintRateGuard._guards.pop(str(wallet.url), None)


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_checkpoints_quote_before_melt_dispatch() -> None:
    wallet, proofs = _wallet()
    events: list[str] = []

    async def checkpoint(quote_id: str) -> None:
        assert quote_id == "q"
        events.append("checkpoint")

    async def melt(**_kwargs: object) -> MagicMock:
        events.append("melt")
        return MagicMock(state=MeltQuoteState.paid)

    wallet.melt = AsyncMock(side_effect=melt)
    data_patch, invoice_patch = _lnurl_patches()

    with data_patch, invoice_patch:
        await raw_send_to_lnurl(
            wallet,
            proofs,
            "owner@ln.tld",
            "sat",
            amount=1000,
            on_melt_quote=checkpoint,
        )

    assert events == ["checkpoint", "melt"]


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
