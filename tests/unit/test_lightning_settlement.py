import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from cashu.core.base import MintQuoteState, Proof

from routstr.lightning import (
    INVOICE_EXPIRY_GRACE_SECONDS,
    InvoiceRecoverRequest,
    _invoice_settlement_locks,
    _is_outputs_already_signed,
    _is_quote_not_found,
    _mint_invoice_quote,
    check_invoice_payment,
    get_invoice_status,
    recover_invoice,
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
        "created_at": 1,
        "expires_at": 2,
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
async def test_quote_not_found_is_definitively_unpaid() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice(status="pending", expires_at=0)
    session = AsyncMock()
    wallet = Mock(
        get_mint_quote=AsyncMock(
            side_effect=Exception("Mint Error: quote not found (Code: 0)")
        )
    )

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning._reload_invoice_view", AsyncMock()),
    ):
        result = await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    assert result is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Mint Error: quote not found (Code: 10000)",
        "Mint Error: quote not found (Code: 01)",
        "Mint Error: quote not found (Code: 0x10)",
    ],
)
async def test_quote_not_found_without_exact_code_0_is_not_definitively_unpaid(
    message: str,
) -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice(status="pending", expires_at=0)
    session = AsyncMock()
    wallet = Mock(get_mint_quote=AsyncMock(side_effect=Exception(message)))

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning._reload_invoice_view", AsyncMock()),
    ):
        result = await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    assert result is False


@pytest.mark.asyncio
async def test_quote_not_found_case_insensitive() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice(status="pending", expires_at=0)
    session = AsyncMock()
    wallet = Mock(
        get_mint_quote=AsyncMock(
            side_effect=Exception("MINT ERROR: Quote Not Found (code 0)")
        )
    )

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning._reload_invoice_view", AsyncMock()),
    ):
        result = await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    assert result is True


@pytest.mark.asyncio
async def test_unknown_quote_is_definitively_unpaid() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice(status="pending", expires_at=0)
    session = AsyncMock()
    wallet = Mock(
        get_mint_quote=AsyncMock(
            side_effect=Exception("Mint Error: Unknown quote (Code: 50000)")
        )
    )

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning._reload_invoice_view", AsyncMock()),
    ):
        result = await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    assert result is True


@pytest.mark.parametrize(
    "message",
    [
        # legacy wording still requires the exact code 0
        "Mint Error: quote not found (Code: 10000)",
        "Mint Error: quote not found (Code: 50000)",
        "Mint Error: unknown request",
        "connection error: quote endpoint unreachable",
    ],
)
def test_unknown_quote_only_matches_real_quote_missing_errors(message: str) -> None:
    assert not _is_quote_not_found(Exception(message))


@pytest.mark.parametrize(
    "message",
    [
        "Mint Error: Unknown quote (Code: 50000)",
        "MINT ERROR: UNKNOWN QUOTE (CODE 50000)",
        "Mint Error: unknown quote (Code: 11000)",
    ],
)
def test_unknown_quote_wording_is_recognized(message: str) -> None:
    assert _is_quote_not_found(Exception(message))


@pytest.mark.asyncio
async def test_credited_invoice_is_not_minted() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice(status="paid")
    session = AsyncMock()

    with patch("routstr.lightning.get_wallet", AsyncMock()) as get_wallet:
        await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    get_wallet.assert_not_awaited()
    session.commit.assert_awaited_once()
    assert _invoice_settlement_locks == {}


@pytest.mark.asyncio
async def test_ambiguous_invoice_mint_timeout_remains_recoverable() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice()
    session = AsyncMock()
    wallet = Mock(get_mint_quote=AsyncMock(return_value=Mock(paid=True)))
    state_session = AsyncMock()
    state_session.exec.return_value.rowcount = 1

    @asynccontextmanager
    async def owned_session() -> AsyncIterator[AsyncMock]:
        yield state_session

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning.create_session", owned_session),
        patch(
            "routstr.lightning._mint_invoice_quote",
            AsyncMock(side_effect=httpx.TimeoutException("response lost")),
        ),
        patch("routstr.lightning._reload_invoice_view", AsyncMock()),
    ):
        await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    assert invoice.status == "settlement_pending"
    state_session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    # One commit closes the initial read transaction before external I/O.
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_quote_not_found_after_payment_confirmation_is_not_unpaid() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice()
    session = AsyncMock()
    wallet = Mock(get_mint_quote=AsyncMock(return_value=Mock(paid=True)))
    state_session = AsyncMock()
    state_session.exec.return_value.rowcount = 1

    @asynccontextmanager
    async def owned_session() -> AsyncIterator[AsyncMock]:
        yield state_session

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning.create_session", owned_session),
        patch(
            "routstr.lightning._mint_invoice_quote",
            AsyncMock(
                side_effect=Exception("Mint Error: quote not found (Code: 0)")
            ),
        ),
        patch("routstr.lightning._reload_invoice_view", AsyncMock()),
    ):
        result = await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    assert result is False
    assert invoice.status == "settlement_pending"


@pytest.mark.asyncio
async def test_quote_lookup_timeout_is_not_definitively_unpaid() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice(expires_at=0)
    session = AsyncMock()
    wallet = Mock(
        get_mint_quote=AsyncMock(side_effect=httpx.TimeoutException("quote timeout"))
    )

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning._reload_invoice_view", AsyncMock()),
    ):
        result = await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    assert result is False


@pytest.mark.asyncio
async def test_overdue_invoice_does_not_expire_after_ambiguous_quote_lookup() -> None:
    invoice = _invoice(status="pending", expires_at=0)
    session = AsyncMock()
    session.get.return_value = invoice
    check = AsyncMock(return_value=False)

    with patch("routstr.lightning.check_invoice_payment", check):
        response = await get_invoice_status(invoice.id, session)  # type: ignore[arg-type]

    assert response.status == "pending"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_overdue_invoice_expires_only_after_definitive_unpaid_quote() -> None:
    invoice = _invoice(status="pending", expires_at=0)
    session = AsyncMock()
    session.get.return_value = invoice
    check = AsyncMock(return_value=True)

    async def expire(
        candidate: SimpleNamespace, _session: AsyncMock, definitive: bool
    ) -> bool:
        assert definitive is True
        candidate.status = "expired"
        return True

    with (
        patch("routstr.lightning.check_invoice_payment", check),
        patch(
            "routstr.lightning._expire_invoice_if_authoritatively_unpaid",
            side_effect=expire,
        ) as expire_invoice,
    ):
        response = await get_invoice_status(invoice.id, session)  # type: ignore[arg-type]

    assert response.status == "expired"
    expire_invoice.assert_awaited_once_with(invoice, session, True)
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_applies_authoritative_expiry_helper() -> None:
    invoice = _invoice(status="pending", expires_at=0)
    session = AsyncMock()
    result = Mock()
    result.first.return_value = invoice
    session.exec.return_value = result
    check = AsyncMock(return_value=True)

    async def expire(
        candidate: SimpleNamespace, _session: AsyncMock, definitive: bool
    ) -> bool:
        assert definitive is True
        candidate.status = "expired"
        return True

    with (
        patch("routstr.lightning.check_invoice_payment", check),
        patch(
            "routstr.lightning._expire_invoice_if_authoritatively_unpaid",
            side_effect=expire,
        ) as expire_invoice,
    ):
        response = await recover_invoice(
            InvoiceRecoverRequest(bolt11="lnbc-test"), session  # type: ignore[arg-type]
        )

    assert response.status == "expired"
    expire_invoice.assert_awaited_once_with(invoice, session, True)


@pytest.mark.asyncio
async def test_paid_state_write_failure_still_reports_non_expirable_outcome() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice(expires_at=0)
    session = AsyncMock()
    wallet = Mock(
        get_mint_quote=AsyncMock(
            return_value=Mock(paid=True, state=MintQuoteState.paid)
        )
    )

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch(
            "routstr.lightning._mint_invoice_quote",
            AsyncMock(side_effect=httpx.TimeoutException("response lost")),
        ),
        patch(
            "routstr.lightning.create_session",
            side_effect=RuntimeError("database unavailable"),
        ),
        patch("routstr.lightning._reload_invoice_view", AsyncMock()),
    ):
        definitively_unpaid = await check_invoice_payment(
            invoice, session  # type: ignore[arg-type]
        )

    assert definitively_unpaid is False
    assert invoice.status == "pending"


@pytest.mark.asyncio
async def test_settlement_pending_invoice_does_not_expire() -> None:
    invoice = _invoice(status="settlement_pending", expires_at=0)
    session = AsyncMock()
    session.get.return_value = invoice
    check = AsyncMock()

    with patch("routstr.lightning.check_invoice_payment", check):
        response = await get_invoice_status(
            invoice.id, session  # type: ignore[arg-type]
        )

    check.assert_awaited_once_with(invoice, session)
    assert response.status == "settlement_pending"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_invoice_checks_finalize_once_in_process() -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice()
    session = AsyncMock()
    wallet = Mock(get_mint_quote=AsyncMock(return_value=Mock(paid=True)))

    async def refresh(obj: SimpleNamespace) -> None:
        return None

    session.refresh = AsyncMock(side_effect=refresh)

    @asynccontextmanager
    async def owned_session() -> AsyncIterator[AsyncMock]:
        owned = AsyncMock()
        owned.exec.return_value.rowcount = 1
        yield owned

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning.create_session", owned_session),
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
    assert _invoice_settlement_locks == {}


@pytest.mark.asyncio
async def test_expired_invoice_inside_grace_still_reaches_the_mint() -> None:
    now = int(time.time())
    invoice = _invoice(status="expired", expires_at=now - 3600)
    session = AsyncMock()
    session.get.return_value = invoice
    check = AsyncMock(return_value=False)

    with patch("routstr.lightning.check_invoice_payment", check):
        response = await get_invoice_status(invoice.id, session)  # type: ignore[arg-type]

    check.assert_awaited_once()
    assert response.status == "expired"


@pytest.mark.asyncio
async def test_expired_invoice_past_grace_answers_without_mint_io() -> None:
    now = int(time.time())
    invoice = _invoice(
        status="expired", expires_at=now - INVOICE_EXPIRY_GRACE_SECONDS - 1
    )
    session = AsyncMock()
    session.get.return_value = invoice
    check = AsyncMock(return_value=False)

    with patch("routstr.lightning.check_invoice_payment", check):
        status_response = await get_invoice_status(invoice.id, session)  # type: ignore[arg-type]

    check.assert_not_awaited()
    assert status_response.status == "expired"


@pytest.mark.asyncio
async def test_recovery_reaches_the_mint_for_an_invoice_past_grace() -> None:
    now = int(time.time())
    invoice = _invoice(
        status="expired", expires_at=now - INVOICE_EXPIRY_GRACE_SECONDS - 1
    )
    session = AsyncMock()
    result = Mock()
    result.first.return_value = invoice
    session.exec.return_value = result
    check = AsyncMock(return_value=False)

    with patch("routstr.lightning.check_invoice_payment", check):
        response = await recover_invoice(
            InvoiceRecoverRequest(bolt11="lnbc-test"), session  # type: ignore[arg-type]
        )

    check.assert_awaited_once()
    assert response.status == "expired"
