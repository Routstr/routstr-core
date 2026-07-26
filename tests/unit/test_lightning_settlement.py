import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from cashu.core.base import Proof

from routstr.lightning import (
    _invoice_settlement_locks,
    _is_outputs_already_signed,
    _mint_invoice_quote,
    check_invoice_payment,
)
from routstr.wallet import Wallet


def _invoice(**overrides: object) -> SimpleNamespace:
    values = {
        "id": "invoice-1",
        "payment_hash": "quote-1",
        "amount_sats": 100,
        "purpose": "create",
        "status": "pending",
        "paid_at": None,
        "api_key_hash": None,
        "mint_url": "http://mint:3338",
        "balance_limit": None,
        "balance_limit_reset": None,
        "validity_date": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _proof(amount: int, mint_id: str, *, reserved: bool = False) -> Proof:
    return Proof(amount=amount, mint_id=mint_id, reserved=reserved)


def _recovery_wallet(
    error: Exception,
    *,
    proofs_before: list[Proof] | None = None,
    proofs_after: list[Proof] | None = None,
) -> Mock:
    async def load_proofs(*, reload: bool) -> None:
        if wallet.load_proofs.await_count >= 2 and proofs_after is not None:
            wallet.proofs = list(proofs_after)

    wallet = Mock(
        mint=AsyncMock(side_effect=error),
        keysets={"keyset-1": Mock()},
        restore_tokens_for_keyset=AsyncMock(),
        load_proofs=AsyncMock(side_effect=load_proofs),
        proofs=list(proofs_before or []),
    )
    return wallet


@pytest.mark.asyncio
async def test_invoice_mint_recovers_quote_linked_outputs_already_signed() -> None:
    invoice = _invoice()
    wallet = _recovery_wallet(
        Exception("Mint Error: outputs have already been signed before (Code: 11003)"),
        proofs_after=[_proof(100, "quote-1")],
    )

    await _mint_invoice_quote(wallet, invoice)  # type: ignore[arg-type]

    wallet.restore_tokens_for_keyset.assert_awaited_once_with(
        "keyset-1", to=1, batch=25
    )
    assert wallet.load_proofs.await_count == 2


@pytest.mark.asyncio
async def test_invoice_mint_accepts_preloaded_quote_linked_proofs() -> None:
    invoice = _invoice()
    wallet = _recovery_wallet(
        Exception("must not mint"),
        proofs_before=[_proof(64, "quote-1"), _proof(36, "quote-1")],
    )

    await _mint_invoice_quote(wallet, invoice)  # type: ignore[arg-type]

    wallet.mint.assert_not_awaited()
    wallet.restore_tokens_for_keyset.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoice_mint_does_not_accept_unrelated_11003_text() -> None:
    invoice = _invoice()
    error = Exception("backend request 11003 failed")
    wallet = _recovery_wallet(error)

    with pytest.raises(Exception) as caught:
        await _mint_invoice_quote(wallet, invoice)  # type: ignore[arg-type]

    assert caught.value is error
    wallet.restore_tokens_for_keyset.assert_not_awaited()


@pytest.mark.asyncio
async def test_installed_cashu_error_shape_recognizes_realistic_11003_phrase() -> None:
    request = httpx.Request("POST", "http://mint:3338/v1/mint/bolt11")
    response = httpx.Response(
        400,
        request=request,
        json={"detail": "outputs have already been signed before", "code": 11003},
    )

    with pytest.raises(Exception) as caught:
        Wallet.raise_on_error_request(response)

    assert _is_outputs_already_signed(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [0, 99])
async def test_invoice_mint_rejects_empty_or_short_quote_recovery(
    recovered: int,
) -> None:
    invoice = _invoice()
    wallet = _recovery_wallet(
        Exception("Mint Error: outputs already signed (Code: 11003)"),
        proofs_after=[_proof(recovered, "quote-1")] if recovered else [],
    )

    with pytest.raises(RuntimeError, match="expected at least 100"):
        await _mint_invoice_quote(wallet, invoice)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_invoice_mint_rejects_unrelated_concurrent_balance_growth() -> None:
    invoice = _invoice()
    wallet = _recovery_wallet(
        Exception("Mint Error: outputs already signed (Code: 11003)"),
        proofs_after=[_proof(10_000, "different-quote")],
    )

    with pytest.raises(RuntimeError, match="quote-linked recovery returned 0"):
        await _mint_invoice_quote(wallet, invoice)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_non_pending_invoice_is_not_minted() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice(status="expired")
    session = AsyncMock()

    with patch("routstr.lightning.get_wallet", AsyncMock()) as get_wallet:
        await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    get_wallet.assert_not_awaited()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_ambiguous_invoice_mint_timeout_does_not_expose_paid() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice()
    session = AsyncMock()
    wallet = Mock(get_mint_quote=AsyncMock(return_value=Mock(paid=True)))

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch(
            "routstr.lightning._mint_invoice_quote",
            AsyncMock(side_effect=httpx.TimeoutException("response lost")),
        ),
        patch("routstr.lightning._reload_invoice_view", AsyncMock()),
    ):
        await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    assert invoice.status == "pending"
    session.rollback.assert_awaited_once()
    # One commit closes the initial read transaction before external I/O.
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_invoice_checks_finalize_once_in_process() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice()
    session = AsyncMock()
    wallet = Mock(get_mint_quote=AsyncMock(return_value=Mock(paid=True)))

    async def refresh(obj: SimpleNamespace) -> None:
        return None

    session.refresh = AsyncMock(side_effect=refresh)
    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning._mint_invoice_quote", AsyncMock()),
        patch(
            "routstr.lightning._finalize_invoice_settlement",
            AsyncMock(return_value=(True, "b" * 64)),
        ) as finalize,
    ):
        await asyncio.gather(
            check_invoice_payment(invoice, session),  # type: ignore[arg-type]
            check_invoice_payment(invoice, session),  # type: ignore[arg-type]
        )

    assert invoice.status == "paid"
    finalize.assert_awaited_once()
