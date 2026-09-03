"""LNURL payouts must credit the unused melt fee reserve back to the key.

The refund-to-LNURL path in ``balance.py`` zeroes the key's balance before
dispatching the payout, then calls ``send_to_lnurl`` and discards the return
value. But the payout spends only the invoice amount while the ``fee_reserve``
reserved the proofs for a larger fee; the difference returns as melt ``change``
and is currently retained in the node's wallet. These tests pin the two seams:
``raw_send_to_lnurl`` must return the net amount actually spent, and
``balance.py`` must credit the unused reserve back to the key in the correct
unit (including the sat-to-msat conversion).
"""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cashu.core.base import MeltQuoteState
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.balance import refund_wallet_endpoint
from routstr.core.db import ApiKey
from routstr.mint import MintRateGuard
from routstr.payment.lnurl import raw_send_to_lnurl


@pytest.fixture(autouse=True)
def _clear_mint_guards() -> Iterator[None]:
    MintRateGuard._guards.clear()
    yield
    MintRateGuard._guards.clear()


async def _engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    return engine


LNURL_DATA = {
    "callback_url": "https://ln.tld/cb",
    "min_sendable": 1_000,
    "max_sendable": 100_000_000,
}


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_returns_the_net_amount_spent() -> None:
    """The melt's change is the unused reserve; the returned value must be the
    net proofs consumed (selected minus change), not the shrunk invoice amount."""
    proofs = [MagicMock(amount=1_000_000, reserved=False)]
    wallet = MagicMock(url="https://mint.test")
    wallet.melt_quote = AsyncMock(
        side_effect=[
            MagicMock(fee_reserve=10_000, quote="q1", amount=1_000_000),
            MagicMock(fee_reserve=10_000, quote="q2", amount=990_000),
        ]
    )
    wallet.melt = AsyncMock(
        return_value=MagicMock(state=MeltQuoteState.paid, change=[MagicMock(amount=3_000)])
    )
    wallet.get_fees_for_proofs = MagicMock(return_value=0)
    wallet.set_reserved_for_send = AsyncMock()
    wallet.set_reserved_for_melt = AsyncMock()

    with (
        patch("routstr.payment.lnurl.get_lnurl_data", AsyncMock(return_value=LNURL_DATA)),
        patch(
            "routstr.payment.lnurl.get_lnurl_invoice",
            AsyncMock(return_value=("lnbc1...", {})),
        ),
    ):
        paid = await raw_send_to_lnurl(
            wallet, proofs, "owner@ln.tld", "msat", amount=1_000_000
        )

    # selected 1_000_000 msat, change 3_000 msat -> net spent 997_000 msat.
    assert paid == 997_000


@pytest.mark.asyncio
async def test_an_lnurl_payout_returns_the_unused_reserve_to_the_balance() -> None:
    engine = await _engine()
    key = ApiKey(
        hashed_key="refund-key",
        balance=1_000_000,
        refund_address="owner@ln.tld",
        refund_currency="msat",
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()

        with patch(
            "routstr.balance.send_to_lnurl", AsyncMock(return_value=997_000)
        ):
            await refund_wallet_endpoint(
                authorization="Bearer sk-refund-key",
                x_cashu=None,
                session=session,
            )

        await session.refresh(key)
        assert key.balance == 3_000
        assert key.reserved_balance == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_a_sat_payout_converts_the_reserve_back_to_msats() -> None:
    engine = await _engine()
    key = ApiKey(
        hashed_key="refund-key",
        balance=1_000_000,
        refund_address="owner@ln.tld",
        refund_currency="sat",
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()

        # send_to_lnurl is fed 1000 sats and spends 997 sats net.
        with patch("routstr.balance.send_to_lnurl", AsyncMock(return_value=997)):
            await refund_wallet_endpoint(
                authorization="Bearer sk-refund-key",
                x_cashu=None,
                session=session,
            )

        await session.refresh(key)
        # 3 unused sats, credited back in msats.
        assert key.balance == 3_000
        assert key.reserved_balance == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_a_failed_lnurl_payout_still_restores_the_full_balance() -> None:
    engine = await _engine()
    key = ApiKey(
        hashed_key="refund-key",
        balance=1_000,
        refund_address="owner@ln.tld",
        refund_currency="msat",
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()

        with patch(
            "routstr.balance.send_to_lnurl",
            AsyncMock(side_effect=RuntimeError("mint unavailable")),
        ):
            with pytest.raises(HTTPException):
                await refund_wallet_endpoint(
                    authorization="Bearer sk-refund-key",
                    x_cashu=None,
                    session=session,
                )

        await session.refresh(key)
        assert key.balance == 1_000
        assert key.reserved_balance == 0

    await engine.dispose()
