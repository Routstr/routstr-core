"""Restart reconciliation for ambiguous melts, against a real cashu wallet DB.

The ambiguous-melt path in ``execute_bolt11_payment`` re-reserves proofs with
``set_reserved_for_melt(..., quote_id=...)`` after cashu's ``melt()`` clears
both the reservation and the ``melt_id`` on a transport error. These tests
prove, on cashu's actual sqlite store rather than mocks, that the recovery
survives a process restart: a fresh wallet instance on the same database can
still find the proofs by ``melt_id`` — the lookup ``get_melt_quote()`` uses to
invalidate them on "paid" or release them on "unpaid".
"""

from pathlib import Path

import pytest
from cashu.core.base import Proof
from cashu.wallet import crud
from cashu.wallet.wallet import Wallet

pytestmark = pytest.mark.asyncio

QUOTE_ID = "quote-restart-1"


def _proof(secret: str, amount: int = 64) -> Proof:
    return Proof(
        id="009a1f293253e41e",
        amount=amount,
        secret=secret,
        C="02bc9097997d81afb2cc7346b5e4345a9346bd2a506eb7958598a72f0cf85163ea",
    )


async def _wallet(db_dir: Path) -> Wallet:
    # with_db builds the instance and runs migrations locally; nothing here
    # talks to a mint.
    return await Wallet.with_db("https://mint.test", str(db_dir))


async def _seed_ambiguous_melt(wallet: Wallet) -> list[Proof]:
    """Reproduce the exact sequence of an ambiguous melt failure.

    1. Proofs exist and are selected for a melt.
    2. cashu's melt() reserves them with the quote id, then hits a transport
       error and rolls that back — reservation gone, melt_id gone.
    3. Our recovery in execute_bolt11_payment re-reserves with the quote id.
    """
    proofs = [_proof("secret-a"), _proof("secret-b", amount=32)]
    for proof in proofs:
        await crud.store_proof(proof, db=wallet.db)

    await wallet.set_reserved_for_melt(proofs, reserved=True, quote_id=QUOTE_ID)
    # cashu's `except` block in melt():
    await wallet.set_reserved_for_melt(proofs, reserved=False, quote_id=None)
    # our recovery:
    await wallet.set_reserved_for_melt(proofs, reserved=True, quote_id=QUOTE_ID)
    return proofs


async def test_melt_recovery_is_findable_by_quote_after_restart(
    tmp_path: Path,
) -> None:
    wallet = await _wallet(tmp_path)
    await _seed_ambiguous_melt(wallet)

    # "Restart": a brand-new wallet on the same database file, as after a
    # process crash between the melt and any reconciliation.
    restarted = await _wallet(tmp_path)
    found = await crud.get_proofs(db=restarted.db, melt_id=QUOTE_ID)

    # This is get_melt_quote()'s own lookup. If it comes back empty, a "paid"
    # answer can never invalidate these proofs and an "unpaid" answer can
    # never release them — the strand the send-style re-reserve caused.
    assert sorted(p.secret for p in found) == ["secret-a", "secret-b"]
    assert all(p.reserved for p in found)
    assert all(p.melt_id == QUOTE_ID for p in found)


async def test_send_style_reservation_would_not_be_reconcilable(
    tmp_path: Path,
) -> None:
    """The defect the fix removed, demonstrated on the real store."""
    wallet = await _wallet(tmp_path)
    proofs = [_proof("secret-send")]
    for proof in proofs:
        await crud.store_proof(proof, db=wallet.db)

    await wallet.set_reserved_for_melt(proofs, reserved=True, quote_id=QUOTE_ID)
    await wallet.set_reserved_for_melt(proofs, reserved=False, quote_id=None)
    # The old recovery: reserve as a send, no quote association.
    await wallet.set_reserved_for_send(proofs, reserved=True)

    restarted = await _wallet(tmp_path)
    found = await crud.get_proofs(db=restarted.db, melt_id=QUOTE_ID)
    assert found == []  # reconciliation would never see these proofs


async def test_unpaid_reconciliation_releases_recovered_proofs_after_restart(
    tmp_path: Path,
) -> None:
    """The full recovery arc: crash, restart, mint says unpaid, funds usable."""
    wallet = await _wallet(tmp_path)
    await _seed_ambiguous_melt(wallet)

    restarted = await _wallet(tmp_path)
    found = await crud.get_proofs(db=restarted.db, melt_id=QUOTE_ID)
    assert len(found) == 2

    # What get_melt_quote() does on an "unpaid" answer.
    await restarted.set_reserved_for_melt(found, reserved=False, quote_id=None)

    released = await crud.get_proofs(db=restarted.db, melt_id=QUOTE_ID)
    assert released == []
    all_proofs = await crud.get_proofs(db=restarted.db)
    assert len(all_proofs) == 2
    assert all(not p.reserved for p in all_proofs)  # spendable again
