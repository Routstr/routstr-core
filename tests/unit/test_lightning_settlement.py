import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx
import pytest
from cashu.core.base import MintQuoteState, Proof
from cashu.core.split import amount_split

from routstr.lightning import (
    INVOICE_EXPIRY_GRACE_SECONDS,
    InvoiceRecoverRequest,
    _invoice_settlement_locks,
    _is_quote_not_found,
    _mint_invoice_quote,
    check_invoice_payment,
    get_invoice_status,
    recover_invoice,
)
from routstr.mint import MintRateGuard


@pytest.fixture(autouse=True)
def _clear_mint_rate_guards() -> Iterator[None]:
    MintRateGuard._guards.clear()
    yield
    MintRateGuard._guards.clear()


def _invoice(**overrides: object) -> SimpleNamespace:
    values = {
        "id": "invoice-1",
        "payment_hash": "quote-1",
        "amount_sats": 100,
        "purpose": "create",
        "direction": "in",
        "status": "pending",
        "paid_at": None,
        "api_key_hash": None,
        "mint_url": "http://mint:3338",
        "validity_date": None,
        "created_at": 1,
        "expires_at": 2,
        "mint_keyset_id": None,
        "mint_counter": None,
        "mint_outputs": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _proof(amount: int, mint_id: str, *, reserved: bool = False) -> Proof:
    return Proof(amount=amount, mint_id=mint_id, reserved=reserved)


class _RecoveryWallet:
    """A wallet whose proof table is a dict, so a reload drops unwritten links.

    The mint has signed the outputs in ``signed`` (secret -> amount).
    """

    def __init__(
        self,
        stored: dict[str, tuple[int, str | None]],
        signed: dict[str, int] | None = None,
    ) -> None:
        self.stored = dict(stored)
        self.signed = dict(signed or {})
        self.proofs: list[Proof] = []
        self.keyset_id = "keyset-1"
        self.mint = AsyncMock()
        self.generate_secrets_from_to = AsyncMock(side_effect=self._secrets)
        self._construct_outputs = Mock(return_value=(["blinded"], []))
        self.restore_promises = AsyncMock(side_effect=self._restore)
        self.invalidate = AsyncMock()

        @asynccontextmanager
        async def connect() -> AsyncIterator[Mock]:
            yield Mock()

        self.db = Mock(connect=connect)

    async def _secrets(self, first: int, last: int, *, keyset_id: str):  # type: ignore[no-untyped-def]
        indices = range(first, last + 1)
        return (
            [f"out-{i}" for i in indices],
            [f"r-{i}" for i in indices],
            [f"path-{i}" for i in indices],
        )

    async def load_proofs(self, *, reload: bool) -> None:
        self.proofs = [
            Proof(amount=amount, secret=secret, mint_id=mint_id)
            for secret, (amount, mint_id) in self.stored.items()
        ]

    async def _restore(self, outputs, secrets, rs, paths):  # type: ignore[no-untyped-def]
        restored = [
            Proof(amount=self.signed[secret], secret=secret)
            for secret in secrets
            if secret in self.signed
        ]
        for proof in restored:
            self.stored[proof.secret] = (proof.amount, None)
        return len(restored), restored


def _linking(wallet: _RecoveryWallet, fail_first: bool = False) -> AsyncMock:
    calls = {"n": 0}

    async def update(proof: Proof, *, mint_id: str, conn: object) -> None:
        calls["n"] += 1
        if fail_first and calls["n"] == 1:
            raise RuntimeError("link write failed")
        wallet.stored[proof.secret] = (proof.amount, mint_id)

    return AsyncMock(side_effect=update)


def _recorded() -> SimpleNamespace:
    return _invoice(mint_keyset_id="keyset-1", mint_counter=18, mint_outputs=2)


def _counters(
    reserved_at: int, high: int, log: list[str]
) -> tuple[AsyncMock, AsyncMock]:
    async def bump(
        *, db: object, keyset_id: str, by: int = 1, skip: bool = False
    ) -> int:
        log.append("read" if skip else f"reserve {by}")
        return high if skip else reserved_at

    async def set_counter(*, db: object, keyset_id: str, counter: int) -> None:
        log.append(f"counter {counter}")

    return AsyncMock(side_effect=bump), AsyncMock(side_effect=set_counter)


@pytest.mark.asyncio
async def test_invoice_mint_accepts_preloaded_quote_linked_proofs() -> None:
    wallet = _RecoveryWallet({"a": (64, "quote-1"), "b": (36, "quote-1")})

    await _mint_invoice_quote(wallet, _invoice())  # type: ignore[arg-type]

    wallet.mint.assert_not_awaited()
    wallet.restore_promises.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_mint_reserves_and_records_its_outputs_before_sending() -> None:
    wallet = _RecoveryWallet({})
    log: list[str] = []

    async def record(*_: object) -> None:
        log.append("record")

    async def mint(*_: object, **__: object) -> None:
        log.append("mint")
        raise ConnectionResetError("reply lost")

    session = AsyncMock(exec=AsyncMock(side_effect=record))
    wallet.mint = AsyncMock(side_effect=mint)
    bump, set_counter = _counters(reserved_at=18, high=30, log=log)

    @asynccontextmanager
    async def create_session() -> AsyncIterator[AsyncMock]:
        yield session

    with (
        patch("routstr.lightning.create_session", create_session),
        patch("routstr.lightning.bump_secret_derivation", bump),
        patch("routstr.lightning.set_secret_derivation", set_counter),
        pytest.raises(ConnectionResetError),
    ):
        await _mint_invoice_quote(wallet, _invoice())  # type: ignore[arg-type]

    assert log == ["reserve 3", "record", "read", "counter 18", "mint", "counter 30"]
    params = session.exec.call_args.args[0].compile().params
    assert (
        params["mint_keyset_id"],
        params["mint_counter"],
        params["mint_outputs"],
    ) == (
        "keyset-1",
        18,
        3,
    )
    wallet.mint.assert_called_once_with(
        100, quote_id="quote-1", split=amount_split(100)
    )
    wallet.restore_promises.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_sends_the_same_recorded_outputs() -> None:
    wallet = _RecoveryWallet({})
    log: list[str] = []

    async def mint(*_: object, **__: object) -> None:
        log.append("mint")
        wallet.stored.update({"out-18": (64, "quote-1"), "out-19": (36, "quote-1")})

    wallet.mint = AsyncMock(side_effect=mint)
    bump, set_counter = _counters(reserved_at=0, high=40, log=log)

    with (
        patch("routstr.lightning.bump_secret_derivation", bump),
        patch("routstr.lightning.set_secret_derivation", set_counter),
    ):
        await _mint_invoice_quote(wallet, _recorded())  # type: ignore[arg-type]

    assert log == ["read", "counter 18", "mint", "counter 40"]
    wallet.restore_promises.assert_not_awaited()


@pytest.mark.asyncio
async def test_issued_quote_links_its_recorded_outputs_already_in_the_wallet() -> None:
    wallet = _RecoveryWallet(
        {"out-18": (64, None), "out-19": (36, None), "other": (100, None)}
    )

    with patch("routstr.lightning.update_proof", _linking(wallet)):
        await _mint_invoice_quote(wallet, _recorded(), issued=True)  # type: ignore[arg-type]

    wallet.mint.assert_not_awaited()
    wallet.restore_promises.assert_not_awaited()
    assert wallet.stored == {
        "out-18": (64, "quote-1"),
        "out-19": (36, "quote-1"),
        "other": (100, None),
    }


@pytest.mark.asyncio
async def test_issued_quote_restores_exactly_its_recorded_outputs() -> None:
    wallet = _RecoveryWallet(
        {"other": (100, None)}, signed={"out-18": 64, "out-19": 36}
    )

    with patch("routstr.lightning.update_proof", _linking(wallet)):
        await _mint_invoice_quote(wallet, _recorded(), issued=True)  # type: ignore[arg-type]

    wallet.mint.assert_not_awaited()
    wallet.generate_secrets_from_to.assert_awaited_once_with(
        18, 19, keyset_id="keyset-1"
    )
    assert wallet.restore_promises.await_args is not None
    assert wallet.restore_promises.await_args.args[1] == ["out-18", "out-19"]
    assert wallet.stored == {
        "other": (100, None),
        "out-18": (64, "quote-1"),
        "out-19": (36, "quote-1"),
    }


@pytest.mark.asyncio
async def test_failed_link_is_finished_by_the_next_check_without_a_restore() -> None:
    wallet = _RecoveryWallet({}, signed={"out-18": 64, "out-19": 36})
    update = _linking(wallet, fail_first=True)

    with patch("routstr.lightning.update_proof", update):
        with pytest.raises(RuntimeError, match="link write failed"):
            await _mint_invoice_quote(wallet, _recorded(), issued=True)  # type: ignore[arg-type]
        await _mint_invoice_quote(wallet, _recorded(), issued=True)  # type: ignore[arg-type]

    assert wallet.restore_promises.await_count == 1
    assert wallet.stored == {"out-18": (64, "quote-1"), "out-19": (36, "quote-1")}


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
@pytest.mark.parametrize(
    ("row_status", "counter", "recovers"),
    [
        ("settlement_pending", 18, True),
        ("settlement_pending", None, False),
        ("pending", 18, False),
    ],
)
async def test_issued_quote_recovers_only_a_claimed_recorded_invoice(
    row_status: str, counter: int | None, recovers: bool
) -> None:
    _invoice_settlement_locks.clear()
    invoice = _invoice(
        status=row_status,
        mint_keyset_id="keyset-1",
        mint_counter=counter,
        mint_outputs=2,
    )
    session = AsyncMock()
    issued = Mock(paid=False, state=MintQuoteState.issued)
    wallet = Mock(get_mint_quote=AsyncMock(return_value=issued))
    mint_quote = AsyncMock(side_effect=httpx.TimeoutException("mint still slow"))

    with (
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning._mint_invoice_quote", mint_quote),
        patch("routstr.lightning._reload_invoice_view", AsyncMock()),
    ):
        result = await check_invoice_payment(invoice, session)  # type: ignore[arg-type]

    assert result is False
    if recovers:
        mint_quote.assert_awaited_once_with(wallet, ANY, issued=True)
    else:
        mint_quote.assert_not_awaited()


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
