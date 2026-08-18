import asyncio
import time
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest
from cashu.core.base import Proof
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, update
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey, LightningInvoice
from routstr.lightning import (
    INVOICE_EXPIRY_GRACE_SECONDS,
    INVOICE_WATCH_BATCH_LIMIT,
    _expire_invoice_if_authoritatively_unpaid,
    _expire_overdue_invoices,
    _finalize_invoice_settlement,
    _InvoiceSettlement,
    _process_invoice_watch_batch,
    check_invoice_payment,
)


def _lightning_invoice(**overrides: object) -> LightningInvoice:
    suffix = uuid.uuid4().hex
    values = {
        "id": f"invoice-{suffix}",
        "bolt11": f"lnbc-{suffix}",
        "amount_sats": 100,
        "description": "settlement test",
        "payment_hash": f"quote-{suffix}",
        "status": "pending",
        "purpose": "create",
        "mint_url": "http://mint:3338",
        "expires_at": int(time.time()) + 3600,
    }
    values.update(overrides)
    return LightningInvoice(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_invoice_read_transaction_closes_before_external_mint_io(
    integration_session: AsyncSession,
) -> None:
    invoice = _lightning_invoice()
    integration_session.add(invoice)
    await integration_session.commit()
    stored = await integration_session.get(LightningInvoice, invoice.id)
    assert stored is not None

    wallet = Mock(get_mint_quote=AsyncMock(return_value=Mock(paid=False)))

    async def get_wallet_without_open_db_transaction(
        *args: object, **kwargs: object
    ) -> Mock:
        assert not integration_session.in_transaction()
        return wallet

    with patch(
        "routstr.lightning.get_wallet", side_effect=get_wallet_without_open_db_transaction
    ):
        await check_invoice_payment(stored, integration_session)

    assert not integration_session.in_transaction()


@pytest.mark.asyncio
async def test_separate_sessions_cas_topup_credit_exactly_once(
    integration_engine: AsyncEngine,
) -> None:
    key_hash = uuid.uuid4().hex
    invoice = _lightning_invoice(
        purpose="topup",
        api_key_hash=key_hash,
        amount_sats=100,
    )
    key = ApiKey(
        hashed_key=key_hash,
        balance=100_000,
        refund_currency="sat",
        refund_mint_url="http://mint:3338",
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add(key)
        seed.add(invoice)
        await seed.commit()

    snapshot_a = _InvoiceSettlement.from_invoice(invoice)
    snapshot_b = _InvoiceSettlement.from_invoice(invoice)
    async with (
        AsyncSession(integration_engine, expire_on_commit=False) as session_a,
        AsyncSession(integration_engine, expire_on_commit=False) as session_b,
    ):
        results = await asyncio.gather(
            _finalize_invoice_settlement(snapshot_a, session_a, 1_700_000_000),
            _finalize_invoice_settlement(snapshot_b, session_b, 1_700_000_001),
        )

    assert sorted(settled for settled, _ in results) == [False, True]
    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored_invoice = await verify.get(LightningInvoice, invoice.id)
        stored_key = await verify.get(ApiKey, key_hash)
        assert stored_invoice is not None
        assert stored_invoice.status == "paid"
        assert stored_key is not None
        assert stored_key.balance == 200_000


@pytest.mark.asyncio
async def test_topup_atomic_increment_preserves_concurrent_balance_mutation(
    integration_engine: AsyncEngine,
) -> None:
    key_hash = uuid.uuid4().hex
    invoice = _lightning_invoice(
        purpose="topup", api_key_hash=key_hash, amount_sats=100
    )
    key = ApiKey(
        hashed_key=key_hash,
        balance=100_000,
        refund_currency="sat",
        refund_mint_url="http://mint:3338",
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add(key)
        seed.add(invoice)
        await seed.commit()

    async def debit_balance(session: AsyncSession) -> None:
        result = await session.exec(  # type: ignore[call-overload]
            update(ApiKey)
            .where(col(ApiKey.hashed_key) == key_hash)
            .values(balance=col(ApiKey.balance) - 10_000)
            .execution_options(synchronize_session=False)
        )
        assert result.rowcount == 1
        await session.commit()

    snapshot = _InvoiceSettlement.from_invoice(invoice)
    async with (
        AsyncSession(integration_engine, expire_on_commit=False) as settlement,
        AsyncSession(integration_engine, expire_on_commit=False) as debit,
    ):
        settlement_result, _ = await asyncio.gather(
            _finalize_invoice_settlement(snapshot, settlement, 1_700_000_000),
            debit_balance(debit),
        )

    assert settlement_result[0]
    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored_key = await verify.get(ApiKey, key_hash)
        assert stored_key is not None
        assert stored_key.balance == 190_000


@pytest.mark.asyncio
async def test_failed_final_commit_rolls_back_claim_and_credit_for_retry(
    integration_engine: AsyncEngine,
) -> None:
    key_hash = uuid.uuid4().hex
    invoice = _lightning_invoice(
        purpose="topup",
        api_key_hash=key_hash,
        amount_sats=100,
    )
    key = ApiKey(
        hashed_key=key_hash,
        balance=100_000,
        refund_currency="sat",
        refund_mint_url="http://mint:3338",
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add(key)
        seed.add(invoice)
        await seed.commit()

    snapshot = _InvoiceSettlement.from_invoice(invoice)
    async with AsyncSession(integration_engine, expire_on_commit=False) as failed:
        with patch.object(
            failed, "commit", AsyncMock(side_effect=Exception("db unavailable"))
        ):
            with pytest.raises(Exception, match="db unavailable"):
                await _finalize_invoice_settlement(snapshot, failed, 1_700_000_000)

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        pending = await verify.get(LightningInvoice, invoice.id)
        unchanged = await verify.get(ApiKey, key_hash)
        assert pending is not None
        assert pending.status == "pending"
        assert unchanged is not None
        assert unchanged.balance == 100_000

    async with AsyncSession(integration_engine, expire_on_commit=False) as retry:
        settled, _ = await _finalize_invoice_settlement(
            snapshot, retry, 1_700_000_001
        )
        assert settled

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        paid = await verify.get(LightningInvoice, invoice.id)
        credited = await verify.get(ApiKey, key_hash)
        assert paid is not None
        assert paid.status == "paid"
        assert credited is not None
        assert credited.balance == 200_000


@pytest.mark.asyncio
async def test_check_invoice_payment_retries_after_mint_success_and_db_failure(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    key_hash = uuid.uuid4().hex
    invoice = _lightning_invoice(
        purpose="topup", api_key_hash=key_hash, amount_sats=100
    )
    key = ApiKey(
        hashed_key=key_hash,
        balance=100_000,
        refund_currency="sat",
        refund_mint_url="http://mint:3338",
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add(key)
        seed.add(invoice)
        await seed.commit()

    wallet = Mock(
        proofs=[],
        keysets={"keyset-1": Mock()},
        load_proofs=AsyncMock(),
        get_mint_quote=AsyncMock(return_value=Mock(paid=True)),
        restore_tokens_for_keyset=AsyncMock(),
    )

    async def mint(amount: int, quote_id: str) -> list[Proof]:
        proofs = [Proof(amount=amount, mint_id=quote_id)]
        wallet.proofs.extend(proofs)
        return proofs

    wallet.mint = AsyncMock(side_effect=mint)

    async with AsyncSession(integration_engine, expire_on_commit=False) as failed:
        stored = await failed.get(LightningInvoice, invoice.id)
        assert stored is not None
        with (
            patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
            patch(
                "routstr.lightning._finalize_invoice_settlement",
                AsyncMock(side_effect=Exception("db unavailable")),
            ),
        ):
            await check_invoice_payment(stored, failed)

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        pending = await verify.get(LightningInvoice, invoice.id)
        unchanged = await verify.get(ApiKey, key_hash)
        assert pending is not None
        assert pending.status == "settlement_pending"
        assert unchanged is not None
        assert unchanged.balance == 100_000

    async with AsyncSession(integration_engine, expire_on_commit=False) as retry:
        stored = await retry.get(LightningInvoice, invoice.id)
        assert stored is not None
        with patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)):
            await check_invoice_payment(stored, retry)

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        paid = await verify.get(LightningInvoice, invoice.id)
        credited = await verify.get(ApiKey, key_hash)
        assert paid is not None
        assert paid.status == "paid"
        assert credited is not None
        assert credited.balance == 200_000

    wallet.mint.assert_awaited_once_with(100, quote_id=invoice.payment_hash)
    wallet.restore_tokens_for_keyset.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_cas_cannot_overwrite_concurrent_paid_invoice(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    invoice = _lightning_invoice(expires_at=0)
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add(invoice)
        await seed.commit()

    async with AsyncSession(integration_engine, expire_on_commit=False) as caller:
        stale = await caller.get(LightningInvoice, invoice.id)
        assert stale is not None
        await caller.commit()

        async with AsyncSession(integration_engine, expire_on_commit=False) as paid:
            result = await paid.exec(  # type: ignore[call-overload]
                update(LightningInvoice)
                .where(col(LightningInvoice.id) == invoice.id)
                .values(status="paid", paid_at=123)
            )
            assert result.rowcount == 1
            await paid.commit()

        expired = await _expire_invoice_if_authoritatively_unpaid(
            stale, caller, True
        )

    assert expired is False
    assert stale.status == "paid"
    assert stale.paid_at == 123
    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored = await verify.get(LightningInvoice, invoice.id)
        assert stored is not None
        assert stored.status == "paid"
        assert stored.paid_at == 123


@pytest.mark.asyncio
async def test_paid_quote_still_credits_after_expiry_claim_wins(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    invoice = _lightning_invoice(expires_at=0)
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add(invoice)
        await seed.commit()

    quote_started = asyncio.Event()
    release_quote = asyncio.Event()

    async def paid_quote_after_expiry(*_args: object, **_kwargs: object) -> Mock:
        quote_started.set()
        await release_quote.wait()
        return Mock(paid=True)

    wallet = Mock(
        get_mint_quote=AsyncMock(side_effect=paid_quote_after_expiry),
        mint=AsyncMock(),
    )

    async with AsyncSession(integration_engine, expire_on_commit=False) as worker:
        observed_pending = await worker.get(LightningInvoice, invoice.id)
        assert observed_pending is not None

        with (
            patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
            patch("routstr.lightning._mint_invoice_quote", AsyncMock()),
        ):
            settlement_task = asyncio.create_task(
                check_invoice_payment(observed_pending, worker)
            )
            await quote_started.wait()

            async with AsyncSession(
                integration_engine, expire_on_commit=False
            ) as expirer:
                expiry_view = await expirer.get(LightningInvoice, invoice.id)
                assert expiry_view is not None
                await expirer.commit()
                assert await _expire_invoice_if_authoritatively_unpaid(
                    expiry_view, expirer, True
                )

            release_quote.set()
            assert await settlement_task is False

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored = await verify.get(LightningInvoice, invoice.id)
        assert stored is not None
        assert stored.status == "paid"
        assert stored.paid_at is not None


@pytest.mark.asyncio
async def test_sweep_expires_only_overdue_pending_invoices(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    now = int(time.time())
    overdue = _lightning_invoice(expires_at=now - 1)
    fresh = _lightning_invoice(expires_at=now + 3600)
    settling = _lightning_invoice(expires_at=now - 1, status="settlement_pending")
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add_all([overdue, fresh, settling])
        await seed.commit()

    await _expire_overdue_invoices(now)

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        for invoice, expected in (
            (overdue, "expired"),
            (fresh, "pending"),
            (settling, "settlement_pending"),
        ):
            stored = await verify.get(LightningInvoice, invoice.id)
            assert stored is not None
            assert stored.status == expected


@pytest.mark.asyncio
async def test_watch_batch_expires_overdue_invoices_and_keeps_settling_rows(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    now = int(time.time())
    overdue = _lightning_invoice(expires_at=now - 1, created_at=now)
    fresh = _lightning_invoice(expires_at=now + 3600, created_at=now)
    settling = _lightning_invoice(
        expires_at=now - 86_400, created_at=now - 86_400, status="settlement_pending"
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add_all([overdue, fresh, settling])
        await seed.commit()

    polled: list[str] = []

    async def record(invoice: LightningInvoice, _session: AsyncSession) -> bool:
        polled.append(invoice.id)
        return False

    async with AsyncSession(integration_engine, expire_on_commit=False) as watcher:
        with patch("routstr.lightning.check_invoice_payment", record):
            await _process_invoice_watch_batch(watcher, now - 10)

    assert fresh.id in polled
    assert settling.id in polled

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored = await verify.get(LightningInvoice, overdue.id)
        assert stored is not None
        assert stored.status == "expired"


@pytest.mark.asyncio
async def test_sweep_cannot_expire_a_row_a_worker_already_claimed(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    now = int(time.time())
    invoice = _lightning_invoice(expires_at=now - 1)
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add(invoice)
        await seed.commit()

    async with AsyncSession(integration_engine, expire_on_commit=False) as claimer:
        claimed = await claimer.exec(  # type: ignore[call-overload]
            update(LightningInvoice)
            .where(col(LightningInvoice.id) == invoice.id)
            .values(status="settlement_pending")
        )
        assert claimed.rowcount == 1
        await claimer.commit()

    assert await _expire_overdue_invoices(now) == 0

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored = await verify.get(LightningInvoice, invoice.id)
        assert stored is not None
        assert stored.status == "settlement_pending"


@pytest.mark.asyncio
async def test_expired_invoice_is_credited_exactly_once(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    key_hash = uuid.uuid4().hex
    invoice = _lightning_invoice(
        status="expired", purpose="topup", api_key_hash=key_hash, amount_sats=100
    )
    key = ApiKey(
        hashed_key=key_hash,
        balance=0,
        refund_currency="sat",
        refund_mint_url="http://mint:3338",
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add(invoice)
        seed.add(key)
        await seed.commit()

    settlement = _InvoiceSettlement.from_invoice(invoice)
    async with AsyncSession(integration_engine, expire_on_commit=False) as first:
        settled, _ = await _finalize_invoice_settlement(settlement, first, 123)
    assert settled is True

    async with AsyncSession(integration_engine, expire_on_commit=False) as second:
        replayed, _ = await _finalize_invoice_settlement(settlement, second, 456)
    assert replayed is False

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored = await verify.get(LightningInvoice, invoice.id)
        assert stored is not None
        assert stored.status == "paid"
        assert stored.paid_at == 123
        credited = await verify.get(ApiKey, key_hash)
        assert credited is not None
        assert credited.balance == 100_000


@pytest.mark.asyncio
async def test_expired_invoice_is_polled_only_inside_the_grace_window(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    now = int(time.time())
    recoverable = _lightning_invoice(
        status="expired", expires_at=now - 3600, created_at=now - 7200
    )
    abandoned = _lightning_invoice(
        status="expired",
        expires_at=now - INVOICE_EXPIRY_GRACE_SECONDS - 1,
        created_at=now - INVOICE_EXPIRY_GRACE_SECONDS - 3600,
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add_all([recoverable, abandoned])
        await seed.commit()

    polled: list[str] = []

    async def record(invoice: LightningInvoice, _session: AsyncSession) -> bool:
        polled.append(invoice.id)
        return False

    async with AsyncSession(integration_engine, expire_on_commit=False) as watcher:
        with patch("routstr.lightning.check_invoice_payment", record):
            await _process_invoice_watch_batch(watcher, now - 601)

    assert recoverable.id in polled
    assert abandoned.id not in polled


@pytest.mark.asyncio
async def test_watcher_credits_a_late_paid_expired_invoice(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    now = int(time.time())
    key_hash = uuid.uuid4().hex
    invoice = _lightning_invoice(
        status="expired",
        purpose="topup",
        api_key_hash=key_hash,
        amount_sats=100,
        expires_at=now - 3600,
        created_at=now - 7200,
    )
    key = ApiKey(
        hashed_key=key_hash,
        balance=0,
        refund_currency="sat",
        refund_mint_url="http://mint:3338",
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add_all([invoice, key])
        await seed.commit()

    async def settle(polled: LightningInvoice, _session: AsyncSession) -> bool:
        settlement = _InvoiceSettlement.from_invoice(polled)
        async with AsyncSession(integration_engine, expire_on_commit=False) as owned:
            await _finalize_invoice_settlement(settlement, owned, now)
        return False

    async with AsyncSession(integration_engine, expire_on_commit=False) as watcher:
        with patch("routstr.lightning.check_invoice_payment", settle):
            await _process_invoice_watch_batch(watcher, now - 601)

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored = await verify.get(LightningInvoice, invoice.id)
        assert stored is not None
        assert stored.status == "paid"
        credited = await verify.get(ApiKey, key_hash)
        assert credited is not None
        assert credited.balance == 100_000


@pytest.mark.asyncio
async def test_recovery_tail_cannot_starve_owed_or_live_invoices(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    now = int(time.time())
    settling = [
        _lightning_invoice(
            status="settlement_pending",
            expires_at=now - 86_400,
            created_at=now - 86_400 - offset,
        )
        for offset in range(50)
    ]
    fresh = [
        _lightning_invoice(expires_at=now + 3600, created_at=now - offset)
        for offset in range(50)
    ]
    tail = [
        _lightning_invoice(
            status="expired",
            expires_at=now - 3600,
            created_at=now - 7200 - offset,
        )
        for offset in range(200)
    ]
    async with AsyncSession(integration_engine, expire_on_commit=False) as seed:
        seed.add_all(settling + fresh + tail)
        await seed.commit()

    polled: list[str] = []

    async def record(invoice: LightningInvoice, _session: AsyncSession) -> bool:
        polled.append(invoice.id)
        return False

    async with AsyncSession(integration_engine, expire_on_commit=False) as watcher:
        with patch("routstr.lightning.check_invoice_payment", record):
            await _process_invoice_watch_batch(watcher, now - 601)

    assert len(polled) == INVOICE_WATCH_BATCH_LIMIT
    assert {inv.id for inv in settling} <= set(polled)
    assert {inv.id for inv in fresh} <= set(polled)
