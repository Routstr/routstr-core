"""Integration tests for Lightning invoice key constraint fields.

Covers two things:
- The three constraint fields (balance_limit, balance_limit_reset, validity_date)
  are persisted on LightningInvoice and survive a DB round-trip.
- create_api_key_from_invoice propagates those fields to the created ApiKey,
  so the constraints are actually enforced when the key is used.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey, LightningInvoice
from routstr.lightning import create_api_key_from_invoice


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
        wallet.mint = AsyncMock(return_value=[])
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

    api_key = await create_api_key_from_invoice(invoice, integration_session)
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

    api_key = await create_api_key_from_invoice(invoice, integration_session)
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

    api_key = await create_api_key_from_invoice(invoice, integration_session)
    await integration_session.commit()

    stored_key = await integration_session.get(ApiKey, api_key.hashed_key)
    assert stored_key is not None
    assert stored_key.validity_date == expiry


@pytest.mark.asyncio
async def test_created_key_without_constraints_has_none_fields(
    integration_session: AsyncSession,
) -> None:
    invoice = _make_invoice()
    integration_session.add(invoice)
    await integration_session.flush()

    api_key = await create_api_key_from_invoice(invoice, integration_session)
    await integration_session.commit()

    stored_key = await integration_session.get(ApiKey, api_key.hashed_key)
    assert stored_key is not None
    assert stored_key.balance_limit is None
    assert stored_key.balance_limit_reset is None
    assert stored_key.validity_date is None
