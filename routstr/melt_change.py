"""Fee reserve change for melts that settle after their own reply.

cashu's ``Wallet.melt`` derives NUT-08 blank outputs from the keyset counter
and unblinds the change only when the melt call itself returns paid. A melt
that comes back pending, times out, or outlives the process is checked later
with ``get_melt_quote``, which marks the quote paid but drops its change.
Recording the counter range right before the melt lets that check store it.
"""

import time

from cashu.core.base import MeltQuote, MeltQuoteState
from cashu.core.helpers import calculate_number_of_blank_outputs
from cashu.wallet.crud import bump_secret_derivation
from cashu.wallet.wallet import Wallet
from sqlmodel import col, delete

from .core import db, get_logger
from .core.db import MeltChangeOutputs
from .core.settings import settings

logger = get_logger(__name__)


async def record_melt_change_outputs(
    wallet: Wallet, quote_id: str, fee_reserve: int
) -> None:
    """Record the blank outputs ``wallet.melt`` is about to derive for change.

    ``fee_reserve`` must be the ``fee_reserve_sat`` handed to that melt. Call
    this right before it under ``wallet_operation_guard`` so nothing else
    advances the keyset counter in between. Best effort: a melt without a
    record only loses change that settles late, as it did before.
    """
    try:
        count = calculate_number_of_blank_outputs(fee_reserve)
        if count < 1:
            return
        counter = await bump_secret_derivation(
            db=wallet.db, keyset_id=wallet.keyset_id, by=0, skip=True
        )
        async with db.create_session() as session:
            await session.merge(
                MeltChangeOutputs(
                    quote_id=quote_id,
                    mint_url=str(wallet.url),
                    unit=wallet.unit.name,
                    keyset_id=wallet.keyset_id,
                    counter=counter,
                    count=count,
                )
            )
            await session.commit()
    except Exception as e:
        logger.warning(
            "Could not record melt change outputs",
            extra={"quote_id": quote_id, "error": str(e)},
        )


async def forget_melt_change_outputs(quote_id: str) -> None:
    """Drop the record of a quote that is final."""
    try:
        async with db.create_session() as session:
            await session.exec(  # type: ignore[call-overload]
                delete(MeltChangeOutputs).where(
                    col(MeltChangeOutputs.quote_id) == quote_id
                )
            )
            await session.commit()
    except Exception as e:
        logger.warning(
            "Could not drop melt change outputs",
            extra={"quote_id": quote_id, "error": str(e)},
        )


async def settle_melt_change(wallet: Wallet, quote: MeltQuote) -> None:
    """Store the change of a quote ``get_melt_quote`` found paid.

    Idempotent: change already in the wallet database is skipped, and the
    record is dropped once the quote is paid and its change stored, or has
    stayed unpaid past the grace window (a fresh melt can report unpaid
    briefly).
    """
    state = getattr(quote, "state", None)
    if state not in (MeltQuoteState.paid, MeltQuoteState.unpaid):
        return
    try:
        async with db.create_session() as session:
            outputs = await session.get(MeltChangeOutputs, quote.quote)
        if outputs is None or (outputs.mint_url, outputs.unit) != (
            str(wallet.url),
            wallet.unit.name,
        ):
            return
        if state == MeltQuoteState.unpaid and outputs.created_at > (
            int(time.time()) - settings.refund_claim_timeout_seconds
        ):
            return
        if state == MeltQuoteState.paid and quote.change:
            await _store_melt_change(wallet, quote, outputs)
    except Exception as e:
        # Keep the record; a later check of this quote (or a restore) can
        # still recover the change.
        logger.error(
            "Could not store late melt change",
            extra={"quote_id": quote.quote, "error": str(e)},
        )
        return
    await forget_melt_change_outputs(quote.quote)


async def _store_melt_change(
    wallet: Wallet, quote: MeltQuote, outputs: MeltChangeOutputs
) -> None:
    # The mint signs the first blank outputs, in order, as cashu's melt assumes.
    change = (quote.change or [])[: outputs.count]
    if not change:
        return
    secrets, rs, paths = await wallet.generate_secrets_from_to(
        outputs.counter, outputs.counter + len(change) - 1, keyset_id=outputs.keyset_id
    )
    stored = await _stored_secrets(wallet, secrets)
    missing = [i for i, secret in enumerate(secrets) if secret not in stored]
    if not missing:
        return
    proofs = await wallet._construct_proofs(
        [change[i] for i in missing],
        [secrets[i] for i in missing],
        [rs[i] for i in missing],
        [paths[i] for i in missing],
    )
    logger.info(
        "Stored change of a late paid melt",
        extra={
            "quote_id": quote.quote,
            "amount": sum(proof.amount for proof in proofs),
            "unit": quote.unit,
        },
    )


async def _stored_secrets(wallet: Wallet, secrets: list[str]) -> set[str]:
    """Secrets the wallet database already holds, spent or not."""
    params = {f"secret_{i}": secret for i, secret in enumerate(secrets)}
    placeholders = ", ".join(f":{name}" for name in params)
    stored: set[str] = set()
    for table in ("proofs", "proofs_used"):
        rows = await wallet.db.fetchall(
            f"SELECT secret FROM {table} WHERE secret IN ({placeholders})", params
        )
        stored.update(row["secret"] for row in rows)
    return stored
