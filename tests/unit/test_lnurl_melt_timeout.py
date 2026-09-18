"""LNURL melt attempts must not misclassify ambiguous payment outcomes."""

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cashu.core.base import MeltQuoteState

from routstr.core.settings import settings
from routstr.mint import MintCooldownError, MintRateGuard
from routstr.payment.lnurl import (
    LNURLError,
    MeltOutcomeAmbiguousError,
    raw_send_to_lnurl,
)


@pytest.fixture(autouse=True)
def _clear_mint_guards() -> Iterator[None]:
    MintRateGuard._guards.clear()
    yield
    MintRateGuard._guards.clear()


LNURL_DATA = {
    "callback_url": "https://ln.tld/cb",
    "min_sendable": 1_000,
    "max_sendable": 100_000_000,
}


QUOTE_AMOUNT_SAT = 999


def _wallet() -> tuple[MagicMock, list[MagicMock]]:
    proofs = [MagicMock(amount=1000, reserved=False)]
    wallet = MagicMock(url="https://mint.test")
    wallet.melt_quote = AsyncMock(
        side_effect=[
            MagicMock(fee_reserve=1, quote="q", amount=1000),
            MagicMock(fee_reserve=1, quote="q", amount=QUOTE_AMOUNT_SAT),
        ]
    )
    wallet.melt = AsyncMock()
    wallet.select_to_send = AsyncMock(return_value=(proofs, None))
    wallet.get_fees_for_proofs = MagicMock(return_value=0)
    wallet.set_reserved_for_melt = AsyncMock()
    wallet.set_reserved_for_send = AsyncMock()
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
async def test_raw_send_to_lnurl_direct_unpaid_is_retry_safe() -> None:
    wallet, proofs = _wallet()
    wallet.melt = AsyncMock(return_value=MagicMock(state=MeltQuoteState.unpaid))
    wallet.get_melt_quote = AsyncMock()
    data_patch, invoice_patch = _lnurl_patches()

    with (
        data_patch,
        invoice_patch,
        pytest.raises(LNURLError, match="confirmed that the melt was unpaid") as raised,
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    assert not isinstance(raised.value, MeltOutcomeAmbiguousError)
    wallet.get_melt_quote.assert_not_awaited()
    wallet.set_reserved_for_send.assert_any_await(proofs, reserved=False)


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_timeout_then_unpaid_remains_ambiguous() -> None:
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
        pytest.raises(MeltOutcomeAmbiguousError, match="immediate unpaid"),
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.get_melt_quote.assert_awaited_once_with("q")
    assert wallet.set_reserved_for_melt.await_count == 2
    wallet.set_reserved_for_melt.assert_awaited_with(
        proofs, reserved=True, quote_id="q"
    )


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_wrapped_transport_unpaid_remains_ambiguous() -> None:
    wallet, proofs = _wallet()

    async def _wrapped_transport_error(**kwargs: object) -> None:
        try:
            raise httpx.ReadTimeout("response lost")
        except httpx.ReadTimeout as transport_error:
            raise Exception("could not pay invoice") from transport_error

    wallet.melt = AsyncMock(side_effect=_wrapped_transport_error)
    wallet.get_melt_quote = AsyncMock(
        return_value=MagicMock(state=MeltQuoteState.unpaid)
    )
    data_patch, invoice_patch = _lnurl_patches()

    with (
        patch.object(settings, "mint_retry_max_attempts", 3),
        data_patch,
        invoice_patch,
        pytest.raises(MeltOutcomeAmbiguousError, match="immediate unpaid"),
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.melt.assert_awaited_once()
    wallet.get_melt_quote.assert_awaited_once_with("q")
    assert wallet.set_reserved_for_melt.await_count == 2
    wallet.set_reserved_for_melt.assert_awaited_with(
        proofs, reserved=True, quote_id="q"
    )


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_does_not_retry_melt_quote_timeout() -> None:
    wallet, proofs = _wallet()
    wallet.melt_quote = AsyncMock(side_effect=httpx.ReadTimeout("response lost"))
    data_patch, invoice_patch = _lnurl_patches()

    with (
        patch.object(settings, "mint_retry_max_attempts", 3),
        data_patch,
        invoice_patch,
        pytest.raises(httpx.TimeoutException),
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.melt_quote.assert_awaited_once()
    wallet.melt.assert_not_awaited()


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
    wallet.set_reserved_for_melt.assert_awaited_once_with(
        proofs, reserved=True, quote_id="q"
    )


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
async def test_pending_then_immediate_unpaid_remains_reserved_and_ambiguous() -> None:
    wallet, proofs = _wallet()
    wallet.melt = AsyncMock(return_value=MagicMock(state=MeltQuoteState.pending))
    wallet.get_melt_quote = AsyncMock(
        return_value=MagicMock(state=MeltQuoteState.unpaid)
    )
    data_patch, invoice_patch = _lnurl_patches()

    with (
        data_patch,
        invoice_patch,
        pytest.raises(MeltOutcomeAmbiguousError, match="immediate unpaid"),
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.set_reserved_for_melt.assert_awaited_once_with(
        proofs, reserved=True, quote_id="q"
    )


@pytest.mark.asyncio
async def test_immediate_unpaid_reservation_failure_stays_ambiguous() -> None:
    wallet, proofs = _wallet()
    wallet.melt = AsyncMock(return_value=MagicMock(state=MeltQuoteState.pending))
    wallet.get_melt_quote = AsyncMock(
        return_value=MagicMock(state=MeltQuoteState.unpaid)
    )
    wallet.set_reserved_for_melt = AsyncMock(side_effect=OSError("db locked"))
    data_patch, invoice_patch = _lnurl_patches()

    with (
        data_patch,
        invoice_patch,
        pytest.raises(MeltOutcomeAmbiguousError, match="could not be restored"),
    ):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)


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
    assert wallet.set_reserved_for_send.await_count == 2
    wallet.set_reserved_for_send.assert_awaited_with(proofs, reserved=False)


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
    assert wallet.set_reserved_for_send.await_count == 2
    wallet.set_reserved_for_send.assert_awaited_with(proofs, reserved=False)
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
