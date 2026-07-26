import asyncio
import time
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest
from cashu.core.base import Proof
from sqlmodel import col, update
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey, LightningInvoice
from routstr.lightning import (
    _finalize_invoice_settlement,
    _InvoiceSettlement,
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
    integration_engine: object,
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
    integration_engine: object,
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
    integration_engine: object,
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
    integration_engine: object,
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
        real_commit = failed.commit
        commit_count = 0

        async def fail_final_commit() -> None:
            nonlocal commit_count
            commit_count += 1
            if commit_count == 2:
                raise Exception("db unavailable")
            await real_commit()

        with (
            patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
            patch.object(failed, "commit", AsyncMock(side_effect=fail_final_commit)),
        ):
            await check_invoice_payment(stored, failed)

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        pending = await verify.get(LightningInvoice, invoice.id)
        unchanged = await verify.get(ApiKey, key_hash)
        assert pending is not None
        assert pending.status == "pending"
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
