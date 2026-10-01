"""End-to-end PostgreSQL coverage for the main application DB.

Runs against a real server so the things SQLite cannot express are actually
exercised: native enum types, INT4 column widths, per-dialect ``ON CONFLICT``,
and genuinely concurrent transactions (SQLite serialises writers, so a
lost-update in a compare-and-swap would never show up there).

Point ``ROUTSTR_TEST_POSTGRES_URL`` at an empty, disposable database::

    docker run -d --name routstr-pg -e POSTGRES_PASSWORD=routstr \\
        -e POSTGRES_USER=routstr -e POSTGRES_DB=routstr -p 55433:5432 \\
        postgres:16-alpine

    ROUTSTR_TEST_POSTGRES_URL=postgresql+asyncpg://routstr:routstr@127.0.0.1:55433/routstr \\
        pytest tests/integration/test_postgres_compatibility.py

Without that variable every test here skips, so the default suite is unchanged.
Each test gets a freshly migrated schema; the public schema is dropped between
tests, so never aim this at a database you care about.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

ROOT = Path(__file__).resolve().parents[2]
POSTGRES_URL = os.environ.get("ROUTSTR_TEST_POSTGRES_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="set ROUTSTR_TEST_POSTGRES_URL to a disposable PostgreSQL database",
    ),
]

MINT = "https://mint.example"
# 21M BTC in millisatoshis: past INT4 by nine orders of magnitude, and past
# INT8 by none. Any monetary column that is still INT4 fails on this value.
HUGE_MSATS = 21_000_000 * 100_000_000 * 1000
# Comfortably past 2038-01-19, when unix seconds overflow INT4.
POST_2038 = 2_600_000_000


def _alembic(*args: str, url: str = POSTGRES_URL) -> subprocess.CompletedProcess[str]:
    """Drive Alembic out-of-process, the way a deployment does."""
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _current_revision() -> str:
    """The stamped revision, isolated from the app's startup logging on stdout."""
    lines = [line.strip() for line in _alembic("current").stdout.splitlines()]
    revisions = [line for line in lines if line and " " not in line.rstrip(" (head)")]
    return revisions[-1].split()[0] if revisions else ""


async def _reset_schema() -> None:
    engine = create_async_engine(POSTGRES_URL, poolclass=None)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest.fixture
def migrated_postgres() -> Iterator[str]:
    """An empty database migrated to head, torn down after the test."""
    asyncio.run(_reset_schema())
    _alembic("upgrade", "head")
    yield POSTGRES_URL
    asyncio.run(_reset_schema())


@pytest_asyncio.fixture
async def pg_session(migrated_postgres: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(migrated_postgres)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


async def _new_key(session: AsyncSession, hashed_key: str, **kwargs: Any) -> Any:
    from routstr.core.db import ApiKey

    key = ApiKey(
        hashed_key=hashed_key,
        refund_mint_url=MINT,
        refund_currency="sat",
        **kwargs,
    )
    session.add(key)
    await session.commit()
    return key


# --------------------------------------------------------------------------
# Alembic chain
# --------------------------------------------------------------------------


def test_full_migration_chain_upgrades_and_downgrades(migrated_postgres: str) -> None:
    """Every revision must run both ways on PostgreSQL, not just on SQLite."""
    head = ScriptDirectory.from_config(
        Config(str(ROOT / "alembic.ini"))
    ).get_current_head()
    assert _current_revision() == head

    _alembic("downgrade", "base")
    assert _current_revision() == ""

    _alembic("upgrade", "head")
    assert _current_revision() == head


def test_migrated_schema_matches_the_orm(migrated_postgres: str) -> None:
    """A migrated database and ``SQLModel.metadata`` must not disagree.

    Drift here is how ``secrets.nsec_state`` shipped: the migration made it
    VARCHAR while the ORM expected a native ``nsecstate`` enum type that
    nothing ever created.
    """
    import routstr.core.db  # noqa: F401 - registers every table

    async def compare() -> list[Any]:
        engine = create_async_engine(migrated_postgres)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(
                    lambda sync_conn: compare_metadata(
                        MigrationContext.configure(
                            sync_conn, opts={"compare_type": True}
                        ),
                        SQLModel.metadata,
                    )
                )
        finally:
            await engine.dispose()

    diffs = asyncio.run(compare())

    def _touches(diff: Any, table: str, column: str | None = None) -> bool:
        rendered = str(diff)
        return table in rendered and (column is None or column in rendered)

    # Known, pre-existing and backend-independent (identical on SQLite):
    #   - `settings` is managed by raw SQL, so it is absent from the ORM metadata
    #   - `cashu_transactions.api_key_hashed_key` declares an FK neither backend has
    #   - `cli_tokens.token` carries both a unique constraint and a unique index
    #   - TEXT vs VARCHAR is not a behavioural difference in PostgreSQL
    unexplained = [
        diff
        for diff in diffs
        if not (
            _touches(diff, "settings")
            or _touches(diff, "cashu_transactions", "api_key_hashed_key")
            or _touches(diff, "cli_tokens", "token")
            or ("modify_type" in str(diff) and "TEXT()" in str(diff))
        )
    ]
    assert unexplained == [], unexplained


def test_no_column_that_holds_money_or_time_is_int4(migrated_postgres: str) -> None:
    """INT4 msats caps a balance at ~0.0215 BTC; INT4 unix seconds die in 2038."""

    async def widths() -> list[str]:
        engine = create_async_engine(POSTGRES_URL)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT table_name, column_name FROM information_schema.columns"
                        " WHERE table_schema = 'public' AND data_type = 'integer'"
                    )
                )
                return [f"{row[0]}.{row[1]}" for row in result]
        finally:
            await engine.dispose()

    narrow = set(asyncio.run(widths()))
    # Surrogate keys, foreign keys and a bounded model attribute.
    allowed = {
        "model_paths.id",
        "model_paths.upstream_provider_id",
        "models.context_length",
        "models.upstream_provider_id",
        "routstr_fees.id",
        "secrets.id",
        "settings.id",
        "upstream_providers.id",
    }
    assert narrow <= allowed, f"still INT4: {sorted(narrow - allowed)}"


# --------------------------------------------------------------------------
# Billing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_balance_holds_more_than_int4(pg_session: AsyncSession) -> None:
    """A node holding more than ~0.0215 BTC must not fail to write its balance."""
    from routstr.core.db import ApiKey, total_user_liability

    await _new_key(pg_session, "whale", balance=HUGE_MSATS, total_spent=HUGE_MSATS)

    stored = await pg_session.get(ApiKey, "whale")
    assert stored is not None
    assert stored.balance == HUGE_MSATS
    assert stored.total_spent == HUGE_MSATS
    assert await total_user_liability(pg_session) == HUGE_MSATS


@pytest.mark.asyncio
async def test_long_dated_expiries_round_trip(pg_session: AsyncSession) -> None:
    """Expiry timestamps past 2038 must survive the round trip."""
    from routstr.core.db import ApiKey

    await _new_key(
        pg_session,
        "long-lived",
        balance=1_000,
        key_expiry_time=POST_2038,
        validity_date=POST_2038,
    )
    stored = await pg_session.get(ApiKey, "long-lived")
    assert stored is not None
    assert stored.key_expiry_time == POST_2038
    assert stored.validity_date == POST_2038


@pytest.mark.asyncio
async def test_balance_aggregates_across_mint_and_unit(
    pg_session: AsyncSession,
) -> None:
    from routstr.core.db import (
        balance_for_mint_and_unit,
        balances_by_mint_and_unit,
        user_liability_for_mint_and_unit,
    )

    await _new_key(pg_session, "a", balance=3_000)
    await _new_key(pg_session, "b", balance=4_500)

    assert await balance_for_mint_and_unit(pg_session, MINT, "sat") == 7_500
    assert await user_liability_for_mint_and_unit(pg_session, MINT, "sat") == 7_500
    assert await balances_by_mint_and_unit(pg_session, [MINT], ["sat"]) == {
        (MINT, "sat"): 7_500
    }


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_overspend(migrated_postgres: str) -> None:
    """The reservation compare-and-swap must hold under real concurrency.

    PostgreSQL runs these two transactions at the same time; SQLite would have
    serialised them and hidden a lost update.
    """
    from fastapi import HTTPException

    from routstr.auth import pay_for_request
    from routstr.core.db import ApiKey

    engine = create_async_engine(migrated_postgres)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            await _new_key(setup, "contended", balance=1_000)

        barrier = asyncio.Barrier(2)

        async def reserve(amount: int) -> int:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                key = await session.get(ApiKey, "contended")
                assert key is not None
                await barrier.wait()  # Both read the same pre-reservation balance.
                try:
                    await pay_for_request(key, amount, session)
                except HTTPException as exc:
                    assert exc.status_code == 402
                    return 0
                return 1

        # Both want 600 of a 1000 balance; exactly one may win.
        won = await asyncio.gather(reserve(600), reserve(600))
        assert sum(won) == 1

        async with AsyncSession(engine, expire_on_commit=False) as check:
            key = await check.get(ApiKey, "contended")
            assert key is not None
            assert key.reserved_balance == 600
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_billing_settlement_and_replay(
    pg_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routstr.auth import adjust_payment_for_tokens, pay_for_request
    from routstr.payment.cost_calculation import CostData

    key = await _new_key(pg_session, "billing", balance=5_000_000_000)
    reservation = await pay_for_request(key, 3_000_000_000, pg_session)
    cost = CostData(
        base_msats=0,
        input_msats=1_000_000_000,
        output_msats=1_500_000_000,
        total_msats=2_500_000_000,
        total_usd=0.0,
        input_tokens=50,
        output_tokens=50,
    )

    async def calculated_cost(*args: Any, **kwargs: Any) -> CostData:
        return cost

    monkeypatch.setattr("routstr.auth.calculate_cost", calculated_cost)
    for _ in range(2):
        await adjust_payment_for_tokens(
            key,
            {
                "model": "test-model",
                "usage": {"prompt_tokens": 50, "completion_tokens": 50},
            },
            pg_session,
            reservation.reserved_msats,
            None,
            None,
            reservation,
        )
        await pg_session.refresh(key)
        assert key.balance == 2_500_000_000
        assert key.total_spent == 2_500_000_000
        assert key.reserved_balance == 0


@pytest.mark.asyncio
async def test_refund_release_and_settle_are_idempotent(
    pg_session: AsyncSession,
) -> None:
    from routstr.refund import open_claim, release, settle

    key = await _new_key(pg_session, "claim", balance=5_000_000_000)
    claim = await open_claim(pg_session, key, method="cashu", destination=None)
    await pg_session.refresh(key)
    assert key.balance == 0 and claim.amount_msats == 5_000_000_000
    assert await release(pg_session, claim)
    assert not await release(pg_session, claim)
    await pg_session.refresh(key)
    assert key.balance == 5_000_000_000
    second = await open_claim(pg_session, key, method="cashu", destination=None)
    assert await settle(pg_session, second, token="test-token")
    assert not await settle(pg_session, second, token="test-token")
    assert not await release(pg_session, second)
    await pg_session.refresh(key)
    assert key.balance == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["create", "topup"])
async def test_invoice_credit_is_atomic_and_idempotent(
    pg_session: AsyncSession, purpose: str
) -> None:
    from routstr.core.db import ApiKey, LightningInvoice
    from routstr.lightning import _finalize_invoice_settlement, _InvoiceSettlement

    if purpose == "topup":
        await _new_key(pg_session, "credit", balance=3_000_000_000)
    invoice = LightningInvoice(
        id="credit-invoice",
        payment_hash="credit-hash",
        bolt11="lnbc-credit",
        amount_sats=1_000_000,
        description="credit",
        purpose=purpose,
        api_key_hash="credit" if purpose == "topup" else None,
        expires_at=POST_2038,
        validity_date=POST_2038,
        mint_url=MINT,
    )
    pg_session.add(invoice)
    await pg_session.commit()
    snapshot = _InvoiceSettlement.from_invoice(invoice)
    paid, key_hash = await _finalize_invoice_settlement(snapshot, pg_session, POST_2038)
    assert paid and key_hash
    assert await _finalize_invoice_settlement(snapshot, pg_session, POST_2038) == (
        False,
        None,
    )
    key = await pg_session.get(ApiKey, key_hash)
    assert key is not None
    assert key.balance == (4_000_000_000 if purpose == "topup" else 1_000_000_000)


# --------------------------------------------------------------------------
# Reservations
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_reservations_are_released(pg_session: AsyncSession) -> None:
    from routstr.core.db import ApiKey, ReservationRelease, release_stale_reservations

    now = int(time.time())
    await _new_key(pg_session, "resv", balance=10_000, reserved_balance=2_500)
    pg_session.add(
        ReservationRelease(
            id=uuid.uuid4().hex,
            key_hash="resv",
            billing_key_hash="resv",
            reserved_msats=2_500,
            status="active",
            created_at=now - 3_600,
        )
    )
    await pg_session.commit()

    assert await release_stale_reservations(pg_session, max_age_seconds=60) == 1

    released = (await pg_session.exec(select(ReservationRelease))).first()
    assert released is not None
    assert released.status == "released"
    key = await pg_session.get(ApiKey, "resv")
    assert key is not None and key.reserved_balance == 0


@pytest.mark.asyncio
async def test_fresh_reservations_survive_the_sweep(pg_session: AsyncSession) -> None:
    from routstr.core.db import ReservationRelease, release_stale_reservations

    await _new_key(pg_session, "fresh", balance=10_000, reserved_balance=1_000)
    pg_session.add(
        ReservationRelease(
            id=uuid.uuid4().hex,
            key_hash="fresh",
            billing_key_hash="fresh",
            reserved_msats=1_000,
            status="active",
            created_at=int(time.time()),
        )
    )
    await pg_session.commit()

    assert await release_stale_reservations(pg_session, max_age_seconds=3_600) == 0


@pytest.mark.asyncio
async def test_reset_all_reserved_balances(pg_session: AsyncSession) -> None:
    from routstr.core.db import ApiKey, reset_all_reserved_balances

    await _new_key(pg_session, "r1", balance=5_000, reserved_balance=500)
    await reset_all_reserved_balances(pg_session)

    key = await pg_session.get(ApiKey, "r1")
    assert key is not None
    assert key.reserved_balance == 0
    assert key.reserved_at is None


# --------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_one_open_refund_per_key(pg_session: AsyncSession) -> None:
    """The partial unique index must be enforced by PostgreSQL too.

    It is declared with both ``sqlite_where`` and ``postgresql_where``; if the
    PostgreSQL variant were dropped, a key could open two concurrent payouts.
    """
    from sqlalchemy.exc import IntegrityError

    from routstr.core.db import Refund

    await _new_key(pg_session, "refundee", balance=50_000)

    def _refund(status: str) -> Refund:
        return Refund(
            id=uuid.uuid4().hex,
            api_key_hashed_key="refundee",
            method="lightning",
            amount_msats=10_000,
            unit="sat",
            mint_url=MINT,
            status=status,
        )

    pg_session.add(_refund("pending"))
    await pg_session.commit()

    pg_session.add(_refund("ambiguous"))
    with pytest.raises(IntegrityError):
        await pg_session.commit()
    await pg_session.rollback()

    # A closed claim does not occupy the slot.
    pg_session.add(_refund("paid"))
    await pg_session.commit()


@pytest.mark.asyncio
async def test_refund_amount_holds_more_than_int4(pg_session: AsyncSession) -> None:
    from routstr.core.db import Refund

    await _new_key(pg_session, "big-refund", balance=HUGE_MSATS)
    refund = Refund(
        id=uuid.uuid4().hex,
        api_key_hashed_key="big-refund",
        method="cashu",
        amount_msats=HUGE_MSATS,
        unit="sat",
        mint_url=MINT,
        status="pending",
        claimed_at=POST_2038,
    )
    pg_session.add(refund)
    await pg_session.commit()

    stored = await pg_session.get(Refund, refund.id)
    assert stored is not None
    assert stored.amount_msats == HUGE_MSATS
    assert stored.claimed_at == POST_2038


@pytest.mark.asyncio
async def test_total_liability_counts_unresolved_refunds(
    pg_session: AsyncSession,
) -> None:
    from routstr.core.db import Refund, total_user_liability

    await _new_key(pg_session, "liable", balance=1_000)
    pg_session.add(
        Refund(
            id=uuid.uuid4().hex,
            api_key_hashed_key="liable",
            method="lightning",
            amount_msats=250,
            unit="sat",
            mint_url=MINT,
            status="pending",
        )
    )
    await pg_session.commit()

    assert await total_user_liability(pg_session) == 1_250


# --------------------------------------------------------------------------
# Invoices and payouts
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lightning_payout_lifecycle(pg_session: AsyncSession) -> None:
    from routstr.core.db import (
        LightningInvoice,
        list_unsettled_lightning_payouts,
        record_lightning_payout,
        settle_lightning_payout,
    )

    await record_lightning_payout(
        pg_session,
        quote_id="quote-1",
        bolt11="lnbc1payout",
        amount_sats=1_234,
        mint_url=MINT,
        destination="node@example.com",
    )

    unsettled = await list_unsettled_lightning_payouts(
        pg_session, MINT, created_before=int(time.time()) + 60
    )
    assert [row.payment_hash for row in unsettled] == ["quote-1"]

    await settle_lightning_payout(
        pg_session, "quote-1", status="paid", amount_sats=1_234
    )
    assert (
        await list_unsettled_lightning_payouts(
            pg_session, MINT, created_before=int(time.time()) + 60
        )
        == []
    )

    paid = (
        await pg_session.exec(
            select(LightningInvoice).where(LightningInvoice.payment_hash == "quote-1")
        )
    ).one()
    assert paid.status == "paid"
    assert paid.paid_at is not None


@pytest.mark.asyncio
async def test_invoice_unique_constraints_hold(pg_session: AsyncSession) -> None:
    from sqlalchemy.exc import IntegrityError

    from routstr.core.db import LightningInvoice

    def _invoice(invoice_id: str) -> LightningInvoice:
        return LightningInvoice(
            id=invoice_id,
            bolt11="lnbc1duplicate",
            amount_sats=10,
            description="dup",
            payment_hash="hash-dup",
            purpose="topup",
            expires_at=POST_2038,
        )

    pg_session.add(_invoice("inv-1"))
    await pg_session.commit()

    pg_session.add(_invoice("inv-2"))
    with pytest.raises(IntegrityError):
        await pg_session.commit()
    await pg_session.rollback()


@pytest.mark.asyncio
async def test_fee_payout_checkpoint_round_trip(pg_session: AsyncSession) -> None:
    """Accumulate, checkpoint, restore, checkpoint again, complete."""
    from routstr.core.db import (
        accumulate_routstr_fee,
        complete_routstr_fee_payout,
        get_routstr_fee,
        reset_routstr_fee,
        restore_routstr_fee_payout,
    )

    await accumulate_routstr_fee(pg_session, HUGE_MSATS)
    assert (await get_routstr_fee(pg_session)).accumulated_msats == HUGE_MSATS

    assert await reset_routstr_fee(pg_session, 5_000, "q1", MINT, "sat") is True
    fee = await get_routstr_fee(pg_session)
    assert fee.payout_in_progress_msats == 5_000
    assert fee.accumulated_msats == HUGE_MSATS - 5_000

    # A payout that never landed goes back to the balance.
    assert (
        await restore_routstr_fee_payout(pg_session, 5_000, "q1", MINT, "sat") is True
    )
    fee = await get_routstr_fee(pg_session)
    assert fee.payout_in_progress_msats == 0
    assert fee.accumulated_msats == HUGE_MSATS

    assert await reset_routstr_fee(pg_session, 5_000, "q2", MINT, "sat") is True
    assert (
        await complete_routstr_fee_payout(pg_session, 5_000, "q2", MINT, "sat") is True
    )
    fee = await get_routstr_fee(pg_session)
    assert fee.payout_in_progress_msats == 0
    assert fee.total_paid_msats == 5_000
    assert fee.accumulated_msats == HUGE_MSATS - 5_000


@pytest.mark.asyncio
async def test_fee_checkpoint_rejects_a_mismatched_quote(
    pg_session: AsyncSession,
) -> None:
    from routstr.core.db import (
        accumulate_routstr_fee,
        complete_routstr_fee_payout,
        reset_routstr_fee,
    )

    await accumulate_routstr_fee(pg_session, 10_000)
    assert await reset_routstr_fee(pg_session, 4_000, "real", MINT, "sat") is True
    assert (
        await complete_routstr_fee_payout(pg_session, 4_000, "wrong", MINT, "sat")
        is False
    )


# --------------------------------------------------------------------------
# Secrets, transactions and model paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secrets_singleton_round_trips(pg_session: AsyncSession) -> None:
    """``nsec_state`` used to need a native ``nsecstate`` type nothing created."""
    from routstr.core.db import NsecState, Secret, get_secret, set_admin_password

    secret = await get_secret(pg_session)
    assert secret.nsec_state == NsecState.legacy

    await set_admin_password(pg_session, "correct-horse-battery")
    stored = await pg_session.get(Secret, 1)
    assert stored is not None
    assert stored.admin_password_hash

    stored.nsec_state = NsecState.cleared
    pg_session.add(stored)
    await pg_session.commit()

    reread = await get_secret(pg_session)
    assert reread.nsec_state == NsecState.cleared

    # Stored by value, so rows written before the column was typed still load.
    raw = await pg_session.exec(text("SELECT nsec_state FROM secrets WHERE id = 1"))  # type: ignore[call-overload]
    assert raw.one()[0] == "cleared"


@pytest_asyncio.fixture
async def db_bound_to_postgres(
    migrated_postgres: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    """Point ``routstr.core.db``'s module-level engine/URL at the test database.

    Reloading the module is not an option: re-executing it redefines every table
    on the shared ``SQLModel.metadata``. Rebinding the two globals that the
    functions under test read is enough and leaves the mappers alone.
    """
    from routstr.core import db as db_module

    engine = db_module.create_db_engine(migrated_postgres)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "DATABASE_URL", migrated_postgres)
    monkeypatch.setenv("DATABASE_URL", migrated_postgres)
    try:
        yield db_module
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cashu_transaction_write_is_idempotent(db_bound_to_postgres: Any) -> None:
    """The retry path reuses a deterministic id, so a replay must not duplicate."""
    db_module = db_bound_to_postgres

    for _ in range(2):
        assert (
            await db_module.store_cashu_transaction_with_retry(
                token="cashuAtoken",
                amount=HUGE_MSATS,
                unit="msat",
                mint_url=MINT,
                typ="in",
            )
            is True
        )

    async with db_module.create_session() as session:
        rows = (await session.exec(select(db_module.CashuTransaction))).all()
    assert len(rows) == 1
    assert rows[0].amount == HUGE_MSATS


@pytest.mark.asyncio
async def test_model_path_upsert_updates_on_conflict(
    pg_session: AsyncSession,
) -> None:
    """``ON CONFLICT`` is per-dialect; the SQLite construct cannot compile here."""
    from routstr.core.db import ModelPathRow, UpstreamProviderRow
    from routstr.upstream.model_paths import _upsert

    provider = UpstreamProviderRow(
        provider_type="custom",
        base_url="https://upstream.example",
        api_key="k",
        enabled=True,
    )
    pg_session.add(provider)
    await pg_session.commit()
    await pg_session.refresh(provider)

    async def write(slug: str, updated_at: int) -> None:
        statement = _upsert(pg_session).values(
            [
                {
                    "model_id": "gpt-x",
                    "path": "url=https%3A%2F%2Fupstream.example",
                    "provider_slug": slug,
                    "provider_type": "custom",
                    "endpoint_tag": None,
                    "endpoint_name": None,
                    "model_metadata": json.dumps({"slug": slug}),
                    "upstream_provider_id": provider.id,
                    "updated_at": updated_at,
                }
            ]
        )
        await pg_session.execute(
            statement.on_conflict_do_update(
                index_elements=["model_id", "path", "upstream_provider_id"],
                set_={
                    "provider_slug": statement.excluded.provider_slug,
                    "model_metadata": statement.excluded.model_metadata,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )
        await pg_session.commit()

    await write("first", 1_000)
    await write("second", POST_2038)

    rows = (await pg_session.exec(select(ModelPathRow))).all()
    assert len(rows) == 1
    assert rows[0].provider_slug == "second"
    assert rows[0].updated_at == POST_2038


@pytest.mark.asyncio
async def test_dead_keys_are_pruned(pg_session: AsyncSession) -> None:
    from routstr.core.db import ApiKey, prune_dead_api_keys

    await _new_key(pg_session, "dead", balance=0, created_at=int(time.time()) - 10_000)
    await _new_key(pg_session, "alive", balance=1_000)

    assert await prune_dead_api_keys(pg_session, min_age_seconds=60) == 1
    assert await pg_session.get(ApiKey, "dead") is None
    assert await pg_session.get(ApiKey, "alive") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_revision", [False, True])
async def test_postgres_migration_errors_never_stamp_head(
    db_bound_to_postgres: Any, unknown_revision: bool
) -> None:
    from alembic.util.exc import CommandError
    from sqlalchemy.exc import ProgrammingError

    db_module = db_bound_to_postgres
    async with db_module.engine.begin() as conn:
        if unknown_revision:
            await conn.execute(
                text("UPDATE alembic_version SET version_num = 'unknown_revision'")
            )
        else:
            # An empty version table and existing tables triggers duplicate DDL.
            await conn.execute(text("DELETE FROM alembic_version"))
    with pytest.raises((CommandError, ProgrammingError)):
        await asyncio.to_thread(db_module.run_migrations)
    async with db_module.engine.connect() as conn:
        versions = (
            (await conn.execute(text("SELECT version_num FROM alembic_version")))
            .scalars()
            .all()
        )
    assert versions == (["unknown_revision"] if unknown_revision else [])
