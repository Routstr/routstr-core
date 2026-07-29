"""Integration tests for Lightning invoice key constraint fields.

Covers two things:
- The three constraint fields (balance_limit, balance_limit_reset, validity_date)
  are persisted on LightningInvoice and survive a DB round-trip.
- The production-path API-key record helper propagates those fields to the
  created ApiKey, so the constraints are actually enforced when the key is used.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cashu.core.base import Proof
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey, LightningInvoice
from routstr.lightning import _create_api_key_record


def _configure_quote_proof_wallet(wallet: MagicMock) -> None:
    wallet.proofs = []
    wallet.keysets = {}
    wallet.load_proofs = AsyncMock()


def _make_invoice(**kwargs: object) -> LightningInvoice:
    base = dict(
        id="inv_test_001",
        bolt11="lnbc1000n1test",
        amount_sats=1000,
        description="test invoice",
        payment_hash="deadbeef" * 8,
        status="paid",
        purpose="create",
        expires_at=int(time.time()) + 3600,
        paid_at=int(time.time()),
    )
    base.update(kwargs)
    return LightningInvoice(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def mock_wallet_mint() -> object:
    with patch("routstr.lightning.get_wallet") as mock_get_wallet:
        wallet = AsyncMock()
        wallet.proofs = []
        wallet.load_proofs = AsyncMock()

        async def mint(amount: int, quote_id: str) -> list[Proof]:
            proofs = [Proof(amount=amount, mint_id=quote_id)]
            wallet.proofs.extend(proofs)
            return proofs

        wallet.mint = AsyncMock(side_effect=mint)
        mock_get_wallet.return_value = wallet
        yield mock_get_wallet


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoice_persists_balance_limit(
    integration_session: AsyncSession,
) -> None:
    invoice = _make_invoice(balance_limit=5000)
    integration_session.add(invoice)
    await integration_session.commit()

    stored = await integration_session.get(LightningInvoice, invoice.id)
    assert stored is not None
    assert stored.balance_limit == 5000


@pytest.mark.asyncio
async def test_invoice_persists_balance_limit_reset(
    integration_session: AsyncSession,
) -> None:
    invoice = _make_invoice(balance_limit=5000, balance_limit_reset="daily")
    integration_session.add(invoice)
    await integration_session.commit()

    stored = await integration_session.get(LightningInvoice, invoice.id)
    assert stored is not None
    assert stored.balance_limit_reset == "daily"


@pytest.mark.asyncio
async def test_invoice_persists_validity_date(
    integration_session: AsyncSession,
) -> None:
    expiry = int(time.time()) + 86400
    invoice = _make_invoice(validity_date=expiry)
    integration_session.add(invoice)
    await integration_session.commit()

    stored = await integration_session.get(LightningInvoice, invoice.id)
    assert stored is not None
    assert stored.validity_date == expiry


# ---------------------------------------------------------------------------
# Propagation to ApiKey
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_created_key_receives_balance_limit(
    integration_session: AsyncSession,
) -> None:
    invoice = _make_invoice(balance_limit=8000)
    integration_session.add(invoice)
    await integration_session.flush()

    api_key = await _create_api_key_record(invoice, integration_session)
    await integration_session.commit()

    stored_key = await integration_session.get(ApiKey, api_key.hashed_key)
    assert stored_key is not None
    assert stored_key.balance_limit == 8000


@pytest.mark.asyncio
async def test_created_key_receives_balance_limit_reset(
    integration_session: AsyncSession,
) -> None:
    invoice = _make_invoice(balance_limit=8000, balance_limit_reset="monthly")
    integration_session.add(invoice)
    await integration_session.flush()

    api_key = await _create_api_key_record(invoice, integration_session)
    await integration_session.commit()

    stored_key = await integration_session.get(ApiKey, api_key.hashed_key)
    assert stored_key is not None
    assert stored_key.balance_limit_reset == "monthly"


@pytest.mark.asyncio
async def test_created_key_receives_validity_date(
    integration_session: AsyncSession,
) -> None:
    expiry = int(time.time()) + 86400
    invoice = _make_invoice(validity_date=expiry)
    integration_session.add(invoice)
    await integration_session.flush()

    api_key = await _create_api_key_record(invoice, integration_session)
    await integration_session.commit()

    stored_key = await integration_session.get(ApiKey, api_key.hashed_key)
    assert stored_key is not None
    assert stored_key.validity_date == expiry


@pytest.mark.asyncio
async def test_payment_check_releases_connection_during_mint_quote(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    invoice = _make_invoice(id="inv_slow_quote", status="pending", paid_at=None)
    async with AsyncSession(integration_engine, expire_on_commit=False) as setup:
        setup.add(invoice)
        await setup.commit()

    async with AsyncSession(integration_engine, expire_on_commit=False) as session:
        stored = await session.get(LightningInvoice, invoice.id)
        assert stored is not None

        async def quote_status(*args: object, **kwargs: object) -> MagicMock:
            assert integration_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            return MagicMock(paid=False)

        wallet = MagicMock()
        wallet.get_mint_quote = AsyncMock(side_effect=quote_status)
        with patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)):
            from routstr.lightning import check_invoice_payment

            await check_invoice_payment(stored, session)


@pytest.mark.asyncio
async def test_concurrent_payment_checks_mint_and_credit_invoice_once(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    invoice = _make_invoice(id="inv_concurrent", status="pending", paid_at=None)
    async with AsyncSession(integration_engine, expire_on_commit=False) as setup:
        setup.add(invoice)
        await setup.commit()

    wallet = MagicMock()
    _configure_quote_proof_wallet(wallet)
    wallet.get_mint_quote = AsyncMock(return_value=MagicMock(paid=True))

    mint_calls = 0

    async def single_use_mint(*args: object, **kwargs: object) -> list[object]:
        # Real mints enforce single-use quotes: the second concurrent minter
        # gets rejected at the mint, mirroring cashu quote semantics.
        nonlocal mint_calls
        mint_calls += 1
        call_number = mint_calls
        await asyncio.sleep(0.05)
        if call_number > 1:
            raise Exception("quote already issued")
        proof = Proof(amount=invoice.amount_sats, mint_id=invoice.payment_hash)
        wallet.proofs.append(proof)
        return [proof]

    wallet.mint = AsyncMock(side_effect=single_use_mint)

    async with (
        AsyncSession(integration_engine, expire_on_commit=False) as first,
        AsyncSession(integration_engine, expire_on_commit=False) as second,
    ):
        first_invoice = await first.get(LightningInvoice, invoice.id)
        second_invoice = await second.get(LightningInvoice, invoice.id)
        assert first_invoice is not None
        assert second_invoice is not None

        with patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)):
            from routstr.lightning import check_invoice_payment

            await asyncio.gather(
                check_invoice_payment(first_invoice, first),
                check_invoice_payment(second_invoice, second),
            )

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored_invoice = await verify.get(LightningInvoice, invoice.id)
        assert stored_invoice is not None
        assert stored_invoice.status == "paid"
        assert stored_invoice.api_key_hash is not None
        stored_key = await verify.get(ApiKey, stored_invoice.api_key_hash)
        assert stored_key is not None
        assert stored_key.balance == invoice.amount_sats * 1000


@pytest.mark.asyncio
async def test_failed_mint_keeps_invoice_pending_for_retry(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    invoice = _make_invoice(id="inv_mint_failure", status="pending", paid_at=None)
    async with AsyncSession(integration_engine, expire_on_commit=False) as setup:
        setup.add(invoice)
        await setup.commit()

    wallet = MagicMock()
    _configure_quote_proof_wallet(wallet)
    wallet.get_mint_quote = AsyncMock(return_value=MagicMock(paid=True))
    wallet.mint = AsyncMock(side_effect=TimeoutError("mint unavailable"))
    async with AsyncSession(integration_engine, expire_on_commit=False) as session:
        stored = await session.get(LightningInvoice, invoice.id)
        assert stored is not None
        with patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)):
            from routstr.lightning import check_invoice_payment

            await check_invoice_payment(stored, session)

    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored = await verify.get(LightningInvoice, invoice.id)
        assert stored is not None
        assert stored.status == "pending"


@pytest.mark.asyncio
async def test_unpaid_topup_does_not_query_target_key(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    invoice = _make_invoice(
        id="inv_unpaid_topup",
        status="pending",
        paid_at=None,
        purpose="topup",
        api_key_hash="target-key",
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as setup:
        setup.add(invoice)
        await setup.commit()

    wallet = MagicMock()
    wallet.get_mint_quote = AsyncMock(return_value=MagicMock(paid=False))
    create_session = MagicMock(side_effect=RuntimeError("target lookup should not run"))
    async with AsyncSession(integration_engine, expire_on_commit=False) as session:
        stored = await session.get(LightningInvoice, invoice.id)
        assert stored is not None
        with (
            patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
            patch("routstr.lightning.create_session", create_session),
        ):
            from routstr.lightning import check_invoice_payment

            await check_invoice_payment(stored, session)

    wallet.get_mint_quote.assert_awaited_once_with(invoice.payment_hash)
    create_session.assert_not_called()


@pytest.mark.asyncio
async def test_missing_topup_target_is_rejected_before_mint(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    invoice = _make_invoice(
        id="inv_missing_topup_target",
        status="pending",
        paid_at=None,
        purpose="topup",
        api_key_hash="pruned-key",
        expires_at=int(time.time()) - 1,
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as setup:
        setup.add(invoice)
        await setup.commit()

    wallet = MagicMock()
    wallet.get_mint_quote = AsyncMock(return_value=MagicMock(paid=True))
    wallet.mint = AsyncMock()
    async with AsyncSession(integration_engine, expire_on_commit=False) as session:
        stored = await session.get(LightningInvoice, invoice.id)
        assert stored is not None
        with (
            patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
            patch("routstr.lightning.logger.critical") as critical,
        ):
            from routstr.lightning import get_invoice_status

            response = await get_invoice_status(invoice.id, session)

        assert response.status == "reconciliation_required"
        assert stored.status == "reconciliation_required"
        assert stored not in session.dirty
        critical.assert_called_once()

    wallet.mint.assert_not_awaited()
    async with AsyncSession(integration_engine) as verify:
        stored = await verify.get(LightningInvoice, invoice.id)
        assert stored is not None
        assert stored.status == "reconciliation_required"


@pytest.mark.asyncio
async def test_post_mint_db_failure_keeps_invoice_pending_for_reconciliation(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    invoice = _make_invoice(id="inv_finalize_failure", status="pending", paid_at=None)
    sibling = _make_invoice(
        id="inv_finalize_failure_sibling",
        bolt11="lnbc1000n1sibling",
        payment_hash="cafebabe" * 8,
        status="pending",
        paid_at=None,
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as setup:
        setup.add_all([invoice, sibling])
        await setup.commit()

    wallet = MagicMock()
    _configure_quote_proof_wallet(wallet)
    wallet.get_mint_quote = AsyncMock(return_value=MagicMock(paid=True))

    async def successful_mint(*args: object, **kwargs: object) -> list[Proof]:
        proof = Proof(amount=invoice.amount_sats, mint_id=invoice.payment_hash)
        wallet.proofs.append(proof)
        return [proof]

    wallet.mint = AsyncMock(side_effect=successful_mint)
    async with AsyncSession(integration_engine, expire_on_commit=False) as session:
        stored = await session.get(LightningInvoice, invoice.id)
        stored_sibling = await session.get(LightningInvoice, sibling.id)
        assert stored is not None
        assert stored_sibling is not None
        with (
            patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
            patch(
                "routstr.lightning._create_api_key_record",
                AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
        ):
            from routstr.lightning import check_invoice_payment

            await check_invoice_payment(stored, session)

        stored_state = inspect(stored)
        sibling_state = inspect(stored_sibling)
        assert stored_state is not None
        assert sibling_state is not None
        assert stored_state.expired is False
        assert sibling_state.expired is False
        assert stored.status == "pending"
        assert stored_sibling.id == sibling.id

    assert wallet.mint.await_count == 1
    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored = await verify.get(LightningInvoice, invoice.id)
        assert stored is not None
        assert stored.status == "pending"


@pytest.mark.asyncio
async def test_created_key_without_constraints_has_none_fields(
    integration_session: AsyncSession,
) -> None:
    invoice = _make_invoice()
    integration_session.add(invoice)
    await integration_session.flush()

    api_key = await _create_api_key_record(invoice, integration_session)
    await integration_session.commit()

    stored_key = await integration_session.get(ApiKey, api_key.hashed_key)
    assert stored_key is not None
    assert stored_key.balance_limit is None
    assert stored_key.balance_limit_reset is None
    assert stored_key.validity_date is None


@pytest.mark.asyncio
async def test_db_guard_credits_once_when_both_mints_succeed(
    integration_engine: AsyncEngine,
    patched_db_engine: None,
) -> None:
    """Even if the mint fails to enforce single-use quotes and both racers
    mint successfully, the conditional status update must credit exactly once."""
    key = ApiKey(hashed_key="race-key", balance=1_000)
    invoice = _make_invoice(
        id="inv_db_guard",
        status="pending",
        paid_at=None,
        purpose="topup",
        api_key_hash="race-key",
    )
    sibling = _make_invoice(
        id="inv_db_guard_sibling",
        bolt11="lnbc1000n1race-sibling",
        payment_hash="01234567" * 8,
        status="pending",
        paid_at=None,
    )
    async with AsyncSession(integration_engine, expire_on_commit=False) as setup:
        setup.add_all([key, invoice, sibling])
        await setup.commit()

    wallet = MagicMock()
    _configure_quote_proof_wallet(wallet)
    wallet.get_mint_quote = AsyncMock(return_value=MagicMock(paid=True))

    async def always_succeeding_mint(*args: object, **kwargs: object) -> list[Proof]:
        await asyncio.sleep(0.05)
        proof = Proof(amount=invoice.amount_sats, mint_id=invoice.payment_hash)
        wallet.proofs.append(proof)
        return [proof]

    wallet.mint = AsyncMock(side_effect=always_succeeding_mint)

    async with (
        AsyncSession(integration_engine, expire_on_commit=False) as first,
        AsyncSession(integration_engine, expire_on_commit=False) as second,
    ):
        first_invoice = await first.get(LightningInvoice, invoice.id)
        first_sibling = await first.get(LightningInvoice, sibling.id)
        second_invoice = await second.get(LightningInvoice, invoice.id)
        assert first_invoice is not None
        assert first_sibling is not None
        assert second_invoice is not None

        with patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)):
            from routstr.lightning import check_invoice_payment

            await asyncio.gather(
                check_invoice_payment(first_invoice, first),
                check_invoice_payment(second_invoice, second),
            )

        first_state = inspect(first_invoice)
        sibling_state = inspect(first_sibling)
        second_state = inspect(second_invoice)
        assert first_state is not None
        assert sibling_state is not None
        assert second_state is not None
        assert first_state.expired is False
        assert sibling_state.expired is False
        assert second_state.expired is False
        assert first_invoice.id == invoice.id
        assert first_sibling.id == sibling.id
        assert second_invoice.id == invoice.id
        assert first_invoice.status == "paid"
        assert second_invoice.status == "paid"
        assert first_invoice not in first.dirty
        assert second_invoice not in second.dirty

    assert wallet.mint.await_count == 1
    async with AsyncSession(integration_engine, expire_on_commit=False) as verify:
        stored_invoice = await verify.get(LightningInvoice, invoice.id)
        assert stored_invoice is not None
        assert stored_invoice.status == "paid"
        stored_key = await verify.get(ApiKey, "race-key")
        assert stored_key is not None
        assert stored_key.balance == 1_000 + invoice.amount_sats * 1000
