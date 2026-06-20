"""Durable, balance-versioned refund idempotency — core #412.

These tests exercise the full ``POST /refund`` -> topup -> spend -> ``POST /refund``
sequence against a **real in-memory SQLite database** (mints/network mocked).

The bug (#412): the refund endpoint cached the issued refund token keyed by
``sha256(bearer)`` and re-served it whenever ``total_balance <= 0``. After a
refund (token T1, balance debited to 0), a *topup* followed by a *spend* brings
the balance back to 0 — but the cache still holds T1, so a second refund
re-serves the already-spent T1 -> "proofs already spent" / fund loss.

The Lightning topup path is the load-bearing gap: it credits balance from a
background watcher with no bearer/authorization in scope, so a header-keyed
cache invalidation structurally cannot fire there.

The durable fix bumps ``ApiKey.balance_version`` atomically on *every* credit
(cashu topup, Lightning topup, new-key credit) and keys refund idempotency on
``(api_key_hash, balance_version)`` in a DB table, so any credit anywhere
invalidates prior refund tokens regardless of worker or auth context.
"""

from __future__ import annotations

import os
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("CASHU_MINTS", "https://mint.example.com")
os.environ.setdefault("NSEC", "nsec1testkey")

import routstr.balance as balance_mod  # noqa: E402
from routstr.core.db import ApiKey  # noqa: E402
from routstr.lightning import topup_api_key_from_invoice  # noqa: E402
from routstr.wallet import credit_balance  # noqa: E402


@pytest_asyncio.fixture
async def engine():  # type: ignore[no-untyped-def]
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[no-untyped-def]
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s


async def _new_key(session: AsyncSession, hashed_key: str, balance: int) -> ApiKey:
    key = ApiKey(
        hashed_key=hashed_key,
        balance=balance,
        refund_currency="sat",
        refund_mint_url="https://mint.example.com",
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key


async def _spend_to_zero(session: AsyncSession, hashed_key: str) -> None:
    """Simulate the user spending their whole balance (no version bump)."""
    key = await session.get(ApiKey, hashed_key)
    assert key is not None
    key.balance = 0
    session.add(key)
    await session.commit()


def _clear_refund_cache() -> None:
    # Defensively clear the legacy in-process cache if it still exists.
    cache = getattr(balance_mod, "_refund_cache", None)
    if isinstance(cache, dict):
        cache.clear()


# ---------------------------------------------------------------------------
# Token minting fakes: each call returns a fresh, unique token so we can assert
# whether a *new* token was minted (correct) or a *stale* one re-served (bug).
# ---------------------------------------------------------------------------


class TokenMinter:
    def __init__(self) -> None:
        self.counter = 0
        self.minted: list[str] = []

    async def __call__(self, amount: int, unit: str, mint_url: str | None = None) -> str:
        self.counter += 1
        tok = f"cashuMINTED_{self.counter}_amt{amount}"
        self.minted.append(tok)
        return tok


async def _do_refund(session: AsyncSession, bearer: str) -> dict:
    result = await balance_mod.refund_wallet_endpoint(
        authorization=f"Bearer {bearer}",
        x_cashu=None,
        session=session,
    )
    assert isinstance(result, dict), f"expected dict refund result, got {result!r}"
    return result


async def _refund_token_or_none(session: AsyncSession, bearer: str) -> str | None:
    """Return the refund token a /refund call yields, or None if the endpoint
    declines (e.g. 400 'No balance to refund'). The #412 bug manifests as a
    *stale token string* being returned where None (or a fresh token) is
    correct."""
    from fastapi import HTTPException

    try:
        result = await balance_mod.refund_wallet_endpoint(
            authorization=f"Bearer {bearer}",
            x_cashu=None,
            session=session,
        )
    except HTTPException:
        return None
    if isinstance(result, dict):
        return result.get("token")
    return None


# ===========================================================================
# 1. #412 across CASHU topup
# ===========================================================================


@pytest.mark.asyncio
async def test_412_cashu_topup_second_refund_is_not_stale_token(session: AsyncSession) -> None:
    _clear_refund_cache()
    hashed = "cashu412hash"
    bearer = f"sk-{hashed}"
    await _new_key(session, hashed, balance=1000)  # 1000 msats

    minter = TokenMinter()

    async def fake_recieve(token: str) -> tuple[int, str, str]:
        # crediting 1 sat = 1000 msats
        return 1, "sat", "https://mint.example.com"

    with (
        patch("routstr.balance.send_token", minter),
        patch("routstr.balance.get_billing_key", AsyncMock(side_effect=lambda k, s: k)),
        patch("routstr.balance.store_cashu_transaction", AsyncMock()),
        patch("routstr.wallet.recieve_token", AsyncMock(side_effect=fake_recieve)),
        patch("routstr.wallet.store_cashu_transaction", AsyncMock()),
    ):
        # 1) First refund: mints T1, debits balance to 0, caches/stores T1.
        r1 = await _do_refund(session, bearer)
        t1 = r1["token"]
        assert t1 == "cashuMINTED_1_amt1"

        # 2) Cashu topup: credit balance again (user adds funds & uses the key).
        key = await session.get(ApiKey, hashed)
        assert key is not None
        await credit_balance("cashuAtopup", key, session)

        # 3) Spend the topped-up balance back to zero (normal usage).
        await _spend_to_zero(session, hashed)

        # 4) Second refund while balance == 0. The legacy cache serves the
        #    STALE T1 here (fund loss: T1's proofs were already spent/minted
        #    against and the user may have already redeemed it). The durable
        #    fix must NOT re-serve T1 — the topup bumped balance_version, so
        #    the stored T1 is no longer valid at the current version (and with
        #    balance 0 there is nothing to refund -> declines).
        t2 = await _refund_token_or_none(session, bearer)

    assert t2 != t1, (
        f"#412 REGRESSION: second refund re-served stale token {t1!r} "
        f"after a cashu topup + spend. The topup must invalidate T1."
    )


# ===========================================================================
# 2. #412 across LIGHTNING topup  (the load-bearing gap)
# ===========================================================================


@pytest.mark.asyncio
async def test_412_lightning_topup_second_refund_is_not_stale_token(session: AsyncSession) -> None:
    _clear_refund_cache()
    hashed = "ln412hash"
    bearer = f"sk-{hashed}"
    await _new_key(session, hashed, balance=1000)

    minter = TokenMinter()

    # Fake the cashu wallet used by topup_api_key_from_invoice -> wallet.mint
    fake_wallet = AsyncMock()
    fake_wallet.mint = AsyncMock(return_value=None)

    class _Invoice:
        amount_sats = 1
        api_key_hash = hashed
        id = "inv1"
        payment_hash = "ph1"

    with (
        patch("routstr.balance.send_token", minter),
        patch("routstr.balance.get_billing_key", AsyncMock(side_effect=lambda k, s: k)),
        patch("routstr.balance.store_cashu_transaction", AsyncMock()),
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=fake_wallet)),
    ):
        # 1) First refund: mint T1, debit to 0, cache/store T1.
        r1 = await _do_refund(session, bearer)
        t1 = r1["token"]

        # 2) LIGHTNING topup via the background-watcher code path. No bearer in
        #    scope here — a header-keyed cache invalidation cannot fire. The
        #    durable fix must bump balance_version inside this DB txn.
        await topup_api_key_from_invoice(_Invoice(), session)  # type: ignore[arg-type]
        await session.commit()

        # 3) Spend the Lightning-topped-up balance back to zero.
        await _spend_to_zero(session, hashed)

        # 4) Second refund at balance 0: must NOT re-serve the stale T1.
        t2 = await _refund_token_or_none(session, bearer)

    assert t2 != t1, (
        f"#412 REGRESSION (Lightning): second refund re-served stale token "
        f"{t1!r} after a Lightning topup. The Lightning credit must bump "
        f"balance_version so the cached/stored refund token is invalidated."
    )


# ===========================================================================
# 3. In-flight idempotency: two refunds at the SAME balance_version (no
#    intervening credit) return the SAME token and debit the balance once.
# ===========================================================================


@pytest.mark.asyncio
async def test_inflight_idempotency_same_version_same_token(session: AsyncSession) -> None:
    _clear_refund_cache()
    hashed = "inflighthash"
    bearer = f"sk-{hashed}"
    await _new_key(session, hashed, balance=2000)

    minter = TokenMinter()

    with (
        patch("routstr.balance.send_token", minter),
        patch("routstr.balance.get_billing_key", AsyncMock(side_effect=lambda k, s: k)),
        patch("routstr.balance.store_cashu_transaction", AsyncMock()),
    ):
        r1 = await _do_refund(session, bearer)
        # Second refund with NO intervening credit -> same balance_version.
        # The balance is now 0; idempotency must re-serve the same token.
        r2 = await _do_refund(session, bearer)

    assert r1["token"] == r2["token"], (
        "in-flight idempotency broken: two refunds at the same balance_version "
        "with no intervening credit must return the SAME token"
    )
    # Exactly one token minted -> balance debited exactly once.
    assert len(minter.minted) == 1, (
        f"idempotent retry must NOT mint a second token; minted={minter.minted}"
    )
