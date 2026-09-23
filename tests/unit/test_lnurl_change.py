from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from cashu.core.base import BlindedMessage, BlindedSignature, Proof, Unit
from cashu.core.crypto import b_dhke
from cashu.core.models import PostMeltQuoteResponse
from cashu.wallet.v1_api import LedgerAPI
from cashu.wallet.wallet import Wallet as CashuWallet

from routstr.core.settings import settings
from routstr.mint import MintRateGuard
from routstr.wallet import _payout_mint_and_unit


@pytest.mark.asyncio
@pytest.mark.parametrize("unit,scale", [("sat", 1), ("msat", 1000)])
@pytest.mark.parametrize(
    "liability,input_fee,reserve,actual_fee",
    [(0, 0, 0, 0), (0, 7, 10, 3), (300000, 7, 10, 3)],
)
async def test_capped_payout_recovers_all_change_with_real_cashu_sdk(
    unit: str,
    scale: int,
    liability: int,
    input_fee: int,
    reserve: int,
    actual_fee: int,
) -> None:
    MintRateGuard._guards.clear()
    private_key = b_dhke.PrivateKey()
    proof = Proof(
        id="00",
        amount=524288 * scale,
        secret="input-proof",
        C=private_key.public_key.format().hex(),
    )
    w = CashuWallet.__new__(CashuWallet)
    w.url = "https://mint.test"
    w.unit = Unit[unit]
    w.keyset_id = "00"
    w.keysets = {
        "00": SimpleNamespace(
            public_keys={2**i: private_key.public_key for i in range(40)}
        )
    }
    w.proofs = [proof]
    w.db = Mock()
    w.get_fees_for_proofs = Mock(return_value=input_fee * scale)
    w.set_reserved_for_send = AsyncMock()
    w.set_reserved_for_melt = AsyncMock()
    w.sign_proofs_inplace_melt = Mock(side_effect=lambda ps, outputs, quote: ps)
    w._store_proofs = AsyncMock()

    async def invalidate(ps: list[Proof]) -> None:
        w.proofs = [p for p in w.proofs if p not in ps]

    w.invalidate = AsyncMock(side_effect=invalidate)
    w.generate_n_secrets = AsyncMock(
        side_effect=lambda n: (
            [f"change-{i}" for i in range(n)],
            [],
            [f"path-{i}" for i in range(n)],
        )
    )
    quotes: dict[str, PostMeltQuoteResponse] = {}

    async def quote(invoice: str) -> PostMeltQuoteResponse:
        amount_msat = int(invoice)
        amount = amount_msat // 1000 if unit == "sat" else amount_msat
        q = PostMeltQuoteResponse(
            quote=str(amount),
            amount=amount,
            unit=unit,
            request=invoice,
            fee_reserve=reserve * scale,
            state="UNPAID",
            expiry=None,
        )
        quotes[q.quote] = q
        return q

    w.melt_quote = AsyncMock(side_effect=quote)
    selected_total = 0
    returned_change = 0
    blank_count = 0
    paid_amount = 0

    async def mint_melt(
        quote_id: str, inputs: list[Proof], outputs: list[BlindedMessage]
    ) -> PostMeltQuoteResponse:
        nonlocal selected_total, returned_change, blank_count, paid_amount
        q = quotes[quote_id]
        selected_total = sum(p.amount for p in inputs)
        paid_amount = q.amount
        blank_count = len(outputs)
        assert q.fee_reserve == reserve * scale
        change = selected_total - q.amount - (input_fee + actual_fee) * scale
        amounts = [2**i for i in range(change.bit_length()) if change & (2**i)]
        signatures = []
        for amount, output in zip(amounts, outputs):
            blinded, _, _ = b_dhke.step2_bob(
                b_dhke.PublicKey(bytes.fromhex(output.B_)), private_key
            )
            signatures.append(
                BlindedSignature(id="00", amount=amount, C_=blinded.format().hex())
            )
        returned_change = sum(s.amount for s in signatures)
        assert returned_change == change
        return q.model_copy(update={"state": "PAID", "change": signatures})

    @asynccontextmanager
    async def session() -> AsyncIterator[Mock]:
        yield Mock()

    with (
        patch.object(settings, "max_payout_sat", 250000),
        patch.object(settings, "min_payout_sat", 210),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=w)),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", return_value=[proof]),
        patch(
            "routstr.wallet.slow_filter_spend_proofs", AsyncMock(return_value=[proof])
        ),
        patch("routstr.wallet.asyncio.sleep", AsyncMock()),
        patch("routstr.wallet.db.create_session", session),
        patch(
            "routstr.wallet.db.total_user_liability",
            AsyncMock(return_value=liability * 1000),
        ),
        patch(
            "routstr.wallet.db.user_liability_for_mint_and_unit",
            AsyncMock(return_value=liability * 1000),
        ),
        patch(
            "routstr.payment.lnurl.get_lnurl_data",
            AsyncMock(
                return_value={
                    "callback_url": "https://ln.test/cb",
                    "min_sendable": 1000,
                    "max_sendable": 10**12,
                }
            ),
        ),
        patch(
            "routstr.payment.lnurl.get_lnurl_invoice",
            AsyncMock(side_effect=lambda callback, amount: (str(amount), {})),
        ),
        patch.object(LedgerAPI, "melt", AsyncMock(side_effect=mint_melt)) as transport,
        patch("cashu.wallet.wallet.update_bolt11_melt_quote", AsyncMock()),
    ):
        await _payout_mint_and_unit(w.url, unit)

    transport.assert_awaited_once()
    assert selected_total == 524288 * scale
    assert blank_count > 0
    assert sum(p.amount for p in w.proofs) == returned_change
    assert all(
        b_dhke.verify(private_key, b_dhke.PublicKey(bytes.fromhex(p.C)), p.secret)
        for p in w.proofs
    )
    net_debit = selected_total - returned_change
    assert net_debit == paid_amount + (input_fee + actual_fee) * scale
    assert net_debit <= min(250000, 524288 - liability) * scale
    assert returned_change >= liability * scale
    if liability == input_fee == reserve == actual_fee == 0:
        assert returned_change == 274288 * scale
        assert net_debit == 250000 * scale
    w._store_proofs.assert_awaited_once()
    MintRateGuard._guards.clear()
