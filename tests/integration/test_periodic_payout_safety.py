"""Money-safety regression coverage for automatic wallet payouts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core import db
from routstr.core.db import ApiKey
from routstr.core.settings import settings
from routstr.wallet import credit_balance, periodic_payout

PRIMARY_MINT = "http://primary:3338"
REFUND_MINT = "http://refund:3338"
PAYOUT_INTERVAL = 987


class _LoopBreak(Exception):
    """Stop the otherwise-infinite payout loop after one cycle."""


def _one_payout_cycle() -> Callable[[float], Coroutine[Any, Any, None]]:
    intervals_seen = 0

    async def sleep(seconds: float) -> None:
        nonlocal intervals_seen
        if seconds == PAYOUT_INTERVAL:
            intervals_seen += 1
            if intervals_seen == 2:
                raise _LoopBreak()

    return sleep


@pytest.mark.asyncio
async def test_cross_mint_liability_is_not_paid_as_owner_profit(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    """Refund preferences must not make primary-mint customer funds payable."""
    async with AsyncSession(integration_engine, expire_on_commit=False) as setup:
        setup.add(
            ApiKey(
                hashed_key="cross-mint-key",
                balance=50_000,
                refund_mint_url=REFUND_MINT,
                refund_currency="sat",
            )
        )
        await setup.commit()

    primary_proof = MagicMock(amount=50)
    raw_send = AsyncMock(return_value=50)

    def proofs_for_mint(
        _wallet: object, mint_url: str, unit: str, **_kwargs: object
    ) -> list[MagicMock]:
        if mint_url == PRIMARY_MINT and unit == "sat":
            return [primary_proof]
        return []

    with (
        patch.object(settings, "cashu_mints", [REFUND_MINT]),
        patch.object(settings, "primary_mint", PRIMARY_MINT),
        patch.object(settings, "receive_ln_address", "owner@ln.test"),
        patch.object(settings, "payout_interval_seconds", PAYOUT_INTERVAL),
        patch.object(settings, "min_payout_sat", 10),
        patch("routstr.wallet.asyncio.sleep", _one_payout_cycle()),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(side_effect=proofs_for_mint),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, _wallet: proofs),
        ),
        patch("routstr.wallet.raw_send_to_lnurl", raw_send),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    raw_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_payout_does_not_send_proofs_whose_liability_commit_is_in_flight(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    """Proof visibility before liability commit must not expose customer funds."""
    key = ApiKey(
        hashed_key="in-flight-topup-key",
        balance=0,
        refund_mint_url=PRIMARY_MINT,
        refund_currency="sat",
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as setup:
        setup.add(key)
        await setup.commit()

    proofs: list[MagicMock] = []
    proof_visible = asyncio.Event()
    finish_redemption = asyncio.Event()
    liability_read = asyncio.Event()

    async def redeem_token(
        token: str,
        destination_mint: str | None = None,
        destination_unit: str | None = None,
    ) -> tuple[int, str, str]:
        proofs.append(MagicMock(amount=200))
        proof_visible.set()
        await finish_redemption.wait()
        return 200, "sat", PRIMARY_MINT

    real_total_liability = db.total_user_liability

    async def read_liability(_session: AsyncSession) -> int:
        async with db.create_session() as snapshot_session:
            value = await real_total_liability(snapshot_session)
        liability_read.set()
        return value

    raw_send = AsyncMock(return_value=200)

    with (
        patch.object(settings, "cashu_mints", []),
        patch.object(settings, "primary_mint", PRIMARY_MINT),
        patch.object(settings, "receive_ln_address", "owner@ln.test"),
        patch.object(settings, "payout_interval_seconds", PAYOUT_INTERVAL),
        patch.object(settings, "min_payout_sat", 10),
        patch("routstr.wallet.asyncio.sleep", _one_payout_cycle()),
        patch("routstr.wallet.recieve_token", AsyncMock(side_effect=redeem_token)),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(side_effect=lambda *_args, **_kwargs: list(proofs)),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda visible, _wallet: visible),
        ),
        patch(
            "routstr.wallet.db.total_user_liability",
            AsyncMock(side_effect=read_liability),
        ),
        patch("routstr.wallet.raw_send_to_lnurl", raw_send),
    ):
        async with AsyncSession(integration_engine, expire_on_commit=False) as credit_session:
            stored_key = await credit_session.get(ApiKey, key.hashed_key)
            assert stored_key is not None
            credit_task = asyncio.create_task(
                credit_balance("cashu-token", stored_key, credit_session)
            )
            await asyncio.wait_for(proof_visible.wait(), timeout=2)

            payout_task = asyncio.create_task(periodic_payout())
            try:
                await asyncio.wait_for(liability_read.wait(), timeout=0.1)
                liability_was_read_while_crediting = True
            except TimeoutError:
                liability_was_read_while_crediting = False

            finish_redemption.set()
            await asyncio.wait_for(credit_task, timeout=2)

            with pytest.raises(_LoopBreak):
                await asyncio.wait_for(payout_task, timeout=2)

    assert liability_was_read_while_crediting is False
    raw_send.assert_not_awaited()
