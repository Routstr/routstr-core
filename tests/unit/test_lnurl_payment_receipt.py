from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from cashu.core.base import MeltQuoteState

from routstr.mint import MintRateGuard
from routstr.payment.lnurl import (
    LNURLPaymentReceipt,
    MeltOutcomeAmbiguousError,
    lnurl_payment_receipt,
    raw_send_to_lnurl,
)
from routstr.wallet import recover_lnurl_payment_receipt


@pytest.fixture(autouse=True)
def _clear_mint_guards() -> Iterator[None]:
    MintRateGuard._guards.clear()
    yield
    MintRateGuard._guards.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", ["sat", "msat"])
@pytest.mark.parametrize("routing_fee_sats", [0, 1])
@pytest.mark.parametrize("lost_response", [False, True])
async def test_paid_melt_receipt_includes_actual_debit_and_checkpoints_inputs(
    unit: str, routing_fee_sats: int, lost_response: bool
) -> None:
    scale = 1 if unit == "sat" else 1000
    proof = SimpleNamespace(amount=64 * scale, reserved=False)
    paid = SimpleNamespace(
        state=MeltQuoteState.paid,
        change=[SimpleNamespace(amount=(63 - routing_fee_sats) * scale)],
    )
    events: list[str] = []

    async def checkpoint_quote(quote: str, invoice: str) -> None:
        events.append("quote")

    async def checkpoint_inputs(sent_msats: int, input_msats: int) -> None:
        assert (sent_msats, input_msats) == (1000, 64000)
        events.append("inputs")

    async def melt(**kwargs: object) -> SimpleNamespace:
        assert events == ["quote", "inputs"]
        if lost_response:
            raise httpx.ReadTimeout("response lost")
        return paid

    wallet = SimpleNamespace(
        url="https://mint.test",
        get_fees_for_proofs=Mock(return_value=0),
        melt_quote=AsyncMock(
            side_effect=[
                SimpleNamespace(amount=3 * scale, fee_reserve=2 * scale, quote="large"),
                SimpleNamespace(amount=scale, fee_reserve=2 * scale, quote="small"),
            ]
        ),
        melt=AsyncMock(side_effect=melt),
        get_melt_quote=AsyncMock(return_value=paid),
        set_reserved_for_send=AsyncMock(),
        set_reserved_for_melt=AsyncMock(),
    )
    with (
        patch(
            "routstr.payment.lnurl.get_lnurl_data",
            AsyncMock(
                return_value={
                    "callback_url": "https://ln.test/cb",
                    "min_sendable": 1000,
                    "max_sendable": 1000000,
                }
            ),
        ),
        patch(
            "routstr.payment.lnurl.get_lnurl_invoice",
            AsyncMock(return_value=("invoice", {})),
        ),
    ):
        receipt = await raw_send_to_lnurl(
            wallet,
            [proof],
            "user@ln.test",
            unit,
            3 * scale,
            on_melt_quote=checkpoint_quote,
            on_melt_inputs=checkpoint_inputs,
        )
    assert isinstance(receipt, LNURLPaymentReceipt)
    assert receipt == receipt.sent_msats == 1000
    assert receipt.spent_msats == (1 + routing_fee_sats) * 1000
    wallet.melt.assert_awaited_once()


def test_unknown_change_is_not_assumed_to_be_zero() -> None:
    with pytest.raises(MeltOutcomeAmbiguousError, match="change evidence"):
        lnurl_payment_receipt(1000, 64000, "sat", None, require_change_evidence=True)
    assert lnurl_payment_receipt(1000, 1000, "sat", None).spent_msats == 1000
    # A complete melt response can legitimately omit change when the entire
    # reserve was used; a later status-only quote is not equivalent evidence.
    assert lnurl_payment_receipt(1000, 3000, "sat", None).spent_msats == 3000


def test_change_cannot_create_a_negative_fee() -> None:
    with pytest.raises(MeltOutcomeAmbiguousError):
        lnurl_payment_receipt(1000, 64000, "sat", [SimpleNamespace(amount=64)])


@pytest.mark.asyncio
@pytest.mark.parametrize("change_sats", [62, 63, None])
async def test_wallet_recovers_debit_from_paid_quote_without_another_melt(
    change_sats: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routstr import wallet as wallet_module

    monkeypatch.setattr(
        wallet_module, "_WALLET_OPERATION_LOCK", tmp_path / "wallet.lock"
    )
    wallet = SimpleNamespace(
        get_melt_quote=AsyncMock(
            return_value=SimpleNamespace(
                state=MeltQuoteState.paid,
                change=None
                if change_sats is None
                else [SimpleNamespace(amount=change_sats)],
            )
        ),
        melt=AsyncMock(),
    )
    with patch("routstr.wallet.get_wallet", AsyncMock(return_value=wallet)):
        if change_sats is None:
            with pytest.raises(MeltOutcomeAmbiguousError, match="change evidence"):
                await recover_lnurl_payment_receipt(
                    "https://mint.test", "sat", "quote", 1000, 64000
                )
        else:
            receipt = await recover_lnurl_payment_receipt(
                "https://mint.test", "sat", "quote", 1000, 64000
            )
            assert isinstance(receipt, LNURLPaymentReceipt)
            assert receipt.sent_msats == 1000
            assert receipt.spent_msats == (64 - change_sats) * 1000
    wallet.get_melt_quote.assert_awaited_once_with("quote")
    wallet.melt.assert_not_awaited()
