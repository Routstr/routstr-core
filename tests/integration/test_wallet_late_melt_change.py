"""Fee reserve change of melts that settle after their own reply.

cashu's ``Wallet.melt`` unblinds change only when the melt call itself returns
paid; ``get_melt_quote`` marks a later paid quote settled but drops the change.
These tests run Routstr's melt and status-check paths against cashu's real
sqlite store and real blinding, with only the mint's HTTP answers faked.
"""

from collections.abc import AsyncIterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from cashu.core.base import (
    DLEQ,
    BlindedMessage,
    BlindedSignature,
    MeltQuote,
    MeltQuoteState,
    Proof,
    WalletKeyset,
)
from cashu.core.crypto import b_dhke
from cashu.core.crypto.secp import PrivateKey, PublicKey
from cashu.core.models import PostMeltQuoteResponse
from cashu.wallet import crud
from cashu.wallet.wallet import Wallet

from routstr.core import db
from routstr.core.db import MeltChangeOutputs
from routstr.core.settings import settings
from routstr.payment.lnurl import raw_send_to_lnurl
from routstr.wallet import (
    Bolt11PaymentAmbiguous,
    Bolt11PaymentPlan,
    _execute_bolt11_payment,
    check_bolt11_payment_status,
)

pytestmark = pytest.mark.asyncio

MINT_URL = "https://mint.test"
KEYSET_ID = "009a1f293253e41e"
QUOTE_ID = "late-melt-quote"
INVOICE = "lnbc900n1-late-melt"
AMOUNT = 90
FEE_RESERVE = 10  # four blank outputs
ACTUAL_FEE = 3  # change of 7 = 1 + 2 + 4


class FakeMint:
    """Signs blank outputs like a NUT-08 mint, answering through cashu's API."""

    def __init__(self, *, melt_state: MeltQuoteState) -> None:
        self.key = PrivateKey()
        self.melt_state = melt_state
        self.quote_state = melt_state
        self.change: list[BlindedSignature] = []

    def keyset(self) -> WalletKeyset:
        return WalletKeyset(
            public_keys={2**i: self.key.public_key for i in range(16)},
            unit="sat",
            id=KEYSET_ID,
            mint_url=MINT_URL,
        )

    def _sign(self, amount: int, output: BlindedMessage) -> BlindedSignature:
        C_, e, s = b_dhke.step2_bob(PublicKey(bytes.fromhex(output.B_)), self.key)
        return BlindedSignature(
            id=KEYSET_ID,
            amount=amount,
            C_=C_.format().hex(),
            dleq=DLEQ(e=e.to_hex(), s=s.to_hex()),
        )

    def _response(self, state: MeltQuoteState) -> PostMeltQuoteResponse:
        paid = state == MeltQuoteState.paid
        return PostMeltQuoteResponse(
            quote=QUOTE_ID,
            amount=AMOUNT,
            unit="sat",
            request=INVOICE,
            fee_reserve=FEE_RESERVE,
            state=state.value,
            expiry=None,
            payment_preimage="preimage" if paid else None,
            change=self.change if paid else None,
        )

    async def melt(
        self,
        quote: str,
        proofs: list[Proof],
        outputs: list[BlindedMessage] | None,
        prefer_async: bool | None = None,
    ) -> PostMeltQuoteResponse:
        overpaid = sum(p.amount for p in proofs) - AMOUNT - ACTUAL_FEE
        amounts = [2**i for i in range(overpaid.bit_length()) if overpaid & 2**i]
        self.change = [self._sign(a, o) for a, o in zip(amounts, outputs or [])]
        return self._response(self.melt_state)

    async def get_melt_quote(self, quote: str) -> PostMeltQuoteResponse:
        return self._response(self.quote_state)


async def _wallet(db_dir: Path, mint: FakeMint) -> Wallet:
    wallet = await Wallet.with_db(MINT_URL, str(db_dir))
    keyset = mint.keyset()
    await crud.store_keyset(keyset=keyset, db=wallet.db)
    wallet.keysets = {KEYSET_ID: keyset}
    wallet.keyset_id = KEYSET_ID
    return wallet


async def _seed_inputs(wallet: Wallet) -> None:
    # 100 sats covers amount plus fee reserve exactly, so coin selection
    # never asks the mint to split.
    inputs = [
        Proof(id=KEYSET_ID, amount=amount, secret=f"input-{amount}", C="00")
        for amount in (64, 32, 4)
    ]
    for proof in inputs:
        await crud.store_proof(proof, db=wallet.db)
    await wallet.load_proofs(reload=True)


async def _plan(wallet: Wallet) -> Bolt11PaymentPlan:
    await _seed_inputs(wallet)
    quote = MeltQuote(
        quote=QUOTE_ID,
        method="bolt11",
        request=INVOICE,
        checking_id="",
        unit="sat",
        amount=AMOUNT,
        fee_reserve=FEE_RESERVE,
        state=MeltQuoteState.unpaid,
        mint=MINT_URL,
    )
    await crud.store_bolt11_melt_quote(db=wallet.db, quote=quote)
    return Bolt11PaymentPlan(INVOICE, wallet, wallet.proofs, quote, MINT_URL, "sat")


@pytest_asyncio.fixture
async def fake_mint(patched_db_engine: None) -> AsyncIterator[Any]:
    """Route cashu's mint calls and Routstr's wallet lookup to a fake mint."""
    with ExitStack() as stack:

        def install(mint: FakeMint, wallet: Wallet) -> None:
            stack.enter_context(patch("cashu.wallet.v1_api.LedgerAPI.melt", mint.melt))
            stack.enter_context(
                patch(
                    "cashu.wallet.v1_api.LedgerAPI.get_melt_quote", mint.get_melt_quote
                )
            )
            stack.enter_context(
                patch("routstr.wallet.get_wallet", AsyncMock(return_value=wallet))
            )

        yield install


async def _stored(wallet: Wallet) -> list[Proof]:
    return await crud.get_proofs(db=wallet.db)


async def _record(quote_id: str) -> MeltChangeOutputs | None:
    async with db.create_session() as session:
        return await session.get(MeltChangeOutputs, quote_id)


async def test_late_paid_melt_stores_its_change(tmp_path: Path, fake_mint: Any) -> None:
    mint = FakeMint(melt_state=MeltQuoteState.pending)
    wallet = await _wallet(tmp_path, mint)
    fake_mint(mint, wallet)
    plan = await _plan(wallet)

    with pytest.raises(Bolt11PaymentAmbiguous):
        await _execute_bolt11_payment(plan)

    mint.quote_state = MeltQuoteState.paid
    assert await check_bolt11_payment_status(MINT_URL, "sat", QUOTE_ID) == "paid"

    change = await _stored(wallet)
    assert sorted(p.amount for p in change) == [1, 2, 4]
    for proof in change:
        assert b_dhke.verify(mint.key, PublicKey(bytes.fromhex(proof.C)), proof.secret)
    assert sum(p.amount for p in wallet.proofs) == 7

    # A second check of the same quote stores nothing twice.
    assert await check_bolt11_payment_status(MINT_URL, "sat", QUOTE_ID) == "paid"
    assert sorted(p.amount for p in await _stored(wallet)) == [1, 2, 4]


async def test_late_change_already_stored_is_not_stored_again(
    tmp_path: Path, fake_mint: Any
) -> None:
    """A crash after storing the change but before dropping the record."""
    mint = FakeMint(melt_state=MeltQuoteState.pending)
    wallet = await _wallet(tmp_path, mint)
    fake_mint(mint, wallet)
    plan = await _plan(wallet)
    with pytest.raises(Bolt11PaymentAmbiguous):
        await _execute_bolt11_payment(plan)
    mint.quote_state = MeltQuoteState.paid

    with patch("routstr.melt_change.forget_melt_change_outputs", AsyncMock()):
        assert await check_bolt11_payment_status(MINT_URL, "sat", QUOTE_ID) == "paid"
    assert await _record(QUOTE_ID) is not None

    assert await check_bolt11_payment_status(MINT_URL, "sat", QUOTE_ID) == "paid"
    assert sorted(p.amount for p in await _stored(wallet)) == [1, 2, 4]
    assert await _record(QUOTE_ID) is None


async def test_melt_records_its_blank_outputs_until_the_quote_is_final(
    tmp_path: Path, fake_mint: Any
) -> None:
    mint = FakeMint(melt_state=MeltQuoteState.pending)
    wallet = await _wallet(tmp_path, mint)
    fake_mint(mint, wallet)
    plan = await _plan(wallet)
    counter = await crud.bump_secret_derivation(
        db=wallet.db, keyset_id=KEYSET_ID, by=0, skip=True
    )

    with pytest.raises(Bolt11PaymentAmbiguous):
        await _execute_bolt11_payment(plan)

    record = await _record(QUOTE_ID)
    assert record is not None
    assert (record.mint_url, record.unit) == (MINT_URL, "sat")
    assert (record.keyset_id, record.counter, record.count) == (KEYSET_ID, counter, 4)
    assert await check_bolt11_payment_status(MINT_URL, "sat", QUOTE_ID) == "pending"
    assert await _record(QUOTE_ID) is not None


async def _age_record(quote_id: str, seconds: int) -> None:
    async with db.create_session() as session:
        record = await session.get(MeltChangeOutputs, quote_id)
        assert record is not None
        record.created_at -= seconds
        session.add(record)
        await session.commit()


async def test_unpaid_quote_drops_its_record(tmp_path: Path, fake_mint: Any) -> None:
    mint = FakeMint(melt_state=MeltQuoteState.pending)
    wallet = await _wallet(tmp_path, mint)
    fake_mint(mint, wallet)
    plan = await _plan(wallet)
    with pytest.raises(Bolt11PaymentAmbiguous):
        await _execute_bolt11_payment(plan)

    mint.quote_state = MeltQuoteState.unpaid
    assert await check_bolt11_payment_status(MINT_URL, "sat", QUOTE_ID) == "unpaid"
    # A fresh melt can report unpaid briefly, so the record outlives it.
    assert await _record(QUOTE_ID) is not None

    await _age_record(QUOTE_ID, settings.refund_claim_timeout_seconds + 1)
    assert await check_bolt11_payment_status(MINT_URL, "sat", QUOTE_ID) == "unpaid"
    assert await _record(QUOTE_ID) is None
    stored = await _stored(wallet)
    assert sorted(p.amount for p in stored) == [4, 32, 64]
    assert not any(p.reserved for p in stored)


async def test_briefly_unpaid_quote_still_stores_late_change(
    tmp_path: Path, fake_mint: Any
) -> None:
    mint = FakeMint(melt_state=MeltQuoteState.pending)
    wallet = await _wallet(tmp_path, mint)
    fake_mint(mint, wallet)
    plan = await _plan(wallet)
    with pytest.raises(Bolt11PaymentAmbiguous):
        await _execute_bolt11_payment(plan)

    mint.quote_state = MeltQuoteState.unpaid
    assert await check_bolt11_payment_status(MINT_URL, "sat", QUOTE_ID) == "unpaid"
    before = {p.secret for p in await _stored(wallet)}
    mint.quote_state = MeltQuoteState.paid
    assert await check_bolt11_payment_status(MINT_URL, "sat", QUOTE_ID) == "paid"

    added = [p for p in await _stored(wallet) if p.secret not in before]
    assert sorted(p.amount for p in added) == [1, 2, 4]
    assert await _record(QUOTE_ID) is None


async def test_immediately_paid_melt_keeps_cashu_change_handling(
    tmp_path: Path, fake_mint: Any
) -> None:
    mint = FakeMint(melt_state=MeltQuoteState.paid)
    wallet = await _wallet(tmp_path, mint)
    fake_mint(mint, wallet)
    plan = await _plan(wallet)

    paid, _, _ = await _execute_bolt11_payment(plan)

    assert paid == AMOUNT + ACTUAL_FEE
    assert sorted(p.amount for p in await _stored(wallet)) == [1, 2, 4]
    assert await _record(QUOTE_ID) is None
    # A later status check does not store cashu's change a second time.
    assert await check_bolt11_payment_status(MINT_URL, "sat", QUOTE_ID) == "paid"
    assert sorted(p.amount for p in await _stored(wallet)) == [1, 2, 4]


async def test_late_paid_lnurl_melt_stores_its_change(
    tmp_path: Path, fake_mint: Any
) -> None:
    mint = FakeMint(melt_state=MeltQuoteState.pending)
    wallet = await _wallet(tmp_path, mint)
    fake_mint(mint, wallet)
    await _seed_inputs(wallet)

    async def melt_quote(
        self: Any, payment_request: str, unit: Any, amount_msat: Any = None
    ) -> PostMeltQuoteResponse:
        amount = int(payment_request.rsplit("-", 1)[1]) // 1000
        return PostMeltQuoteResponse(
            quote=QUOTE_ID if amount == AMOUNT else f"quote-{amount}",
            amount=amount,
            unit="sat",
            request=payment_request,
            fee_reserve=FEE_RESERVE,
            state=MeltQuoteState.unpaid.value,
            expiry=None,
        )

    lnurl_data = {
        "callback_url": "https://pay.test/callback",
        "min_sendable": 1000,
        "max_sendable": 1_000_000,
    }

    async def lnurl_invoice(callback_url: str, amount_msat: int) -> tuple[str, Any]:
        return f"{INVOICE}-{amount_msat}", None

    mint.quote_state = MeltQuoteState.paid
    with (
        patch("cashu.wallet.v1_api.LedgerAPI.melt_quote", melt_quote),
        patch(
            "routstr.payment.lnurl.get_lnurl_data", AsyncMock(return_value=lnurl_data)
        ),
        patch("routstr.payment.lnurl.get_lnurl_invoice", lnurl_invoice),
    ):
        paid = await raw_send_to_lnurl(
            wallet, wallet.proofs, "user@pay.test", "sat", amount=100
        )

    assert paid == AMOUNT * 1000
    assert sorted(p.amount for p in await _stored(wallet)) == [1, 2, 4]
    assert await _record(QUOTE_ID) is None
