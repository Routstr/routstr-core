"""Cross-mint swaps run outside the wallet lock, under a bounded budget, and
leave a journal row for every Lightning leg they dispatch."""

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, Mock, patch

import pytest
from cashu.core.base import MeltQuoteState
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr import foreign_mint_swap as fms
from routstr import refund as refund_module
from routstr import wallet
from routstr.core import db
from routstr.core.db import ApiKey, CashuSwap, CashuTransaction, Refund
from routstr.core.settings import settings
from routstr.mint import MintRateGuard, mint_cooldown_remaining
from routstr.payment.lnurl import MeltOutcomeAmbiguousError
from routstr.wallet import (
    Bolt11PaymentAmbiguous,
    ForeignMintSwapError,
    ForeignMintUnavailableError,
    SwapPendingError,
    TokenConsumedError,
    wallet_operation_guard,
)

PRIMARY = "https://primary.example"
FOREIGN = "https://foreign.example"
KEY_HASH = "a" * 64


@pytest.fixture
async def engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def create_session() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(db, "create_session", create_session)
    monkeypatch.setattr(refund_module, "create_session", create_session)
    monkeypatch.setattr(wallet, "_WALLET_OPERATION_LOCK", tmp_path / "op.lock")
    monkeypatch.setattr(settings, "primary_mint", PRIMARY)
    monkeypatch.setattr(settings, "primary_mint_unit", "sat")
    monkeypatch.setattr(settings, "cashu_mints", [PRIMARY])
    monkeypatch.setattr(settings, "foreign_mint_operation_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "foreign_mint_max_concurrency", 4)
    monkeypatch.setattr(fms, "_foreign_slots", None)
    MintRateGuard._guards.clear()
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


async def _make_key(session: AsyncSession, **fields: Any) -> ApiKey:
    fields.setdefault("balance", 0)
    key = ApiKey(hashed_key=KEY_HASH, **fields)
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key


def _proof(amount: int) -> SimpleNamespace:
    return SimpleNamespace(amount=amount, reserved=False, secret=f"s{amount}", id="k")


def _token(amount: int = 1000, mint: str = FOREIGN) -> SimpleNamespace:
    return SimpleNamespace(
        mint=mint, unit="sat", amount=amount, keysets=["k"], proofs=[_proof(amount)]
    )


class _ForeignWallet:
    """Fake wallet for the sender's mint; records that the guard was not held."""

    def __init__(self, fee_reserve: int = 5, melt_state: Any = MeltQuoteState.paid):
        self.fee_reserve = fee_reserve
        self.melt_state = melt_state
        self.guard_depth_seen: list[int] = []
        self.load_mint_keysets = AsyncMock(side_effect=self._observe)
        self.activate_keyset = AsyncMock()
        self._expand_short_keyset_ids = AsyncMock()
        self.verify_proofs_dleq = Mock()
        self.get_fees_for_proofs = Mock(return_value=1)
        self.melt_quote = AsyncMock(side_effect=self._melt_quote)
        self.melt = AsyncMock(side_effect=self._melt)
        self.get_melt_quote = AsyncMock(
            return_value=SimpleNamespace(state=MeltQuoteState.paid)
        )
        self.request_mint = AsyncMock(side_effect=self._request_mint)
        self.mint = AsyncMock(side_effect=self._mint)
        self.serialize_proofs = AsyncMock(return_value="cashuBrefund")
        self.set_reserved_for_send = AsyncMock()
        self.load_proofs = AsyncMock()
        self.available_balance = SimpleNamespace(amount=0)
        self.keysets: dict[str, Any] = {}
        self.proofs: list[Any] = []

    async def _observe(self, *args: Any, **kwargs: Any) -> None:
        self.guard_depth_seen.append(wallet._wallet_operation_depth.get())

    async def _melt_quote(self, invoice: str, amount_msat: int | None = None) -> Any:
        await self._observe()
        amount = int(invoice.rsplit(":", 1)[1])
        return SimpleNamespace(
            quote=f"melt-{amount}", amount=amount, fee_reserve=self.fee_reserve
        )

    async def _melt(self, **kwargs: Any) -> Any:
        await self._observe()
        if isinstance(self.melt_state, BaseException):
            raise self.melt_state
        if self.melt_state == "hang":
            await asyncio.sleep(5)
        return SimpleNamespace(state=self.melt_state)

    async def _request_mint(self, amount: int, memo: str | None = None) -> Any:
        await self._observe()
        return SimpleNamespace(quote=f"mint-{amount}", request=f"lnbc:{amount}")

    async def _mint(self, amount: int, quote_id: str, split: Any = None) -> list[Any]:
        await self._observe()
        return [_proof(amount)]


class _PrimaryWallet:
    def __init__(self, proofs: list[Any] | None = None, fee_reserve: int = 2):
        self.proofs = proofs or [_proof(500), _proof(500)]
        self.fee_reserve = fee_reserve
        self.request_mint = AsyncMock(side_effect=self._request_mint)
        self.mint = AsyncMock(return_value=[_proof(1)])
        self.load_proofs = AsyncMock()
        self.available_balance = SimpleNamespace(amount=0)
        self.keysets: dict[str, Any] = {}
        self.select_to_send = AsyncMock(side_effect=self._select)
        self.get_fees_for_proofs = Mock(return_value=0)
        self.melt_quote = AsyncMock(side_effect=self._melt_quote)

    async def _request_mint(self, amount: int, memo: str | None = None) -> Any:
        return SimpleNamespace(quote=f"mint-{amount}", request=f"lnbc:{amount}")

    async def _select(self, proofs: Any, amount: int, **kwargs: Any) -> Any:
        return proofs, 0

    async def _melt_quote(self, invoice: str, amount_msat: int | None = None) -> Any:
        amount = int(invoice.rsplit(":", 1)[1])
        return SimpleNamespace(
            quote=f"melt-{amount}", amount=amount, fee_reserve=self.fee_reserve
        )


@asynccontextmanager
async def _swap_env(
    foreign: _ForeignWallet,
    primary: _PrimaryWallet,
    token: SimpleNamespace,
) -> AsyncGenerator[None, None]:
    wallets = {FOREIGN: foreign, PRIMARY: primary}

    async def get_wallet(mint_url: str, unit: str = "sat", **kwargs: Any) -> Any:
        return wallets[mint_url]

    async def run_mint_operation(factory: Any, **kwargs: Any) -> Any:
        return await factory()

    with (
        patch.object(fms, "deserialize_token_from_string", return_value=token),
        patch.object(fms, "assert_public_https_origin", AsyncMock()),
        patch.object(fms, "get_wallet", get_wallet),
        patch.object(fms, "run_mint_operation", run_mint_operation),
        patch.object(
            fms,
            "get_proofs_per_mint_and_unit",
            lambda w, m, u, not_reserved=False: list(w.proofs),
        ),
    ):
        yield


async def _swap_rows(session: AsyncSession) -> list[CashuSwap]:
    # Rows are written through other sessions; read with a fresh one so the
    # test session's identity map cannot serve stale copies.
    async with AsyncSession(session.bind, expire_on_commit=False) as fresh:
        return list((await fresh.exec(select(CashuSwap))).all())


async def _refund_row(session: AsyncSession, refund_id: str) -> Refund:
    async with AsyncSession(session.bind, expire_on_commit=False) as fresh:
        row = await fresh.get(Refund, refund_id)
    assert row is not None
    return row


def _mint_recovered() -> None:
    """The melt timeout put the mint on cooldown; the reconciler runs later."""
    MintRateGuard._guards.clear()


# --- foreign-mint budget ---------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_operation_is_single_attempt_with_cooldown(
    engine: AsyncEngine,
) -> None:
    calls = 0

    async def slow() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(5)

    started = time.monotonic()
    with pytest.raises(ForeignMintUnavailableError):
        await fms.run_foreign_mint_operation(slow, mint_url=FOREIGN, op_name="t")
    assert time.monotonic() - started < 2
    assert calls == 1
    assert mint_cooldown_remaining(FOREIGN) > 0
    # Cooling down: refused offline without touching the mint again.
    with pytest.raises(ForeignMintUnavailableError):
        await fms.run_foreign_mint_operation(slow, mint_url=FOREIGN, op_name="t")
    assert calls == 1


@pytest.mark.asyncio
async def test_foreign_operation_refuses_to_run_under_wallet_guard(
    engine: AsyncEngine,
) -> None:
    async with wallet_operation_guard():
        with pytest.raises(RuntimeError):
            await fms.run_foreign_mint_operation(
                AsyncMock(), mint_url=FOREIGN, op_name="t"
            )
        with pytest.raises(RuntimeError):
            async with fms.foreign_mint_lock(FOREIGN):
                pass


@pytest.mark.asyncio
async def test_foreign_budget_is_global_and_fails_fast(engine: AsyncEngine) -> None:
    settings.foreign_mint_max_concurrency = 1
    release = asyncio.Event()

    async def hold() -> None:
        await release.wait()

    holder = asyncio.create_task(
        fms.run_foreign_mint_operation(hold, mint_url=FOREIGN, op_name="hold")
    )
    await asyncio.sleep(0.01)
    other_mint_called = False

    async def other() -> None:
        nonlocal other_mint_called
        other_mint_called = True

    # A different hostname does not get its own budget.
    with pytest.raises(ForeignMintUnavailableError):
        await fms.run_foreign_mint_operation(
            other, mint_url="https://other.example", op_name="o"
        )
    assert not other_mint_called
    release.set()
    await holder


@pytest.mark.asyncio
async def test_foreign_mint_lock_waits_bounded(engine: AsyncEngine) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with fms.foreign_mint_lock(FOREIGN):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await entered.wait()
    with pytest.raises(ForeignMintUnavailableError):
        async with fms.foreign_mint_lock(FOREIGN):
            pass
    release.set()
    await holder


# --- inbound swap -----------------------------------------------------------


@pytest.mark.asyncio
async def test_swap_in_rejects_non_https_mint_before_any_contact(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    get_wallet = AsyncMock()
    with (
        patch.object(
            fms,
            "deserialize_token_from_string",
            return_value=_token(mint="http://foreign.example"),
        ),
        patch.object(fms, "get_wallet", get_wallet),
    ):
        with pytest.raises(ForeignMintSwapError):
            await fms.swap_in_and_credit("cashuAhttp", key, session)
    get_wallet.assert_not_awaited()
    assert await _swap_rows(session) == []


@pytest.mark.asyncio
async def test_swap_in_happy_path_credits_net_and_pins_refund_mint(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    foreign = _ForeignWallet(fee_reserve=5)
    primary = _PrimaryWallet()
    async with _swap_env(foreign, primary, _token(1000)):
        credited = await fms.swap_in_and_credit("cashuAswap", key, session)

    # 1000 sat token, 1 sat input fee, 5 sat fee reserve -> 994 sat minted.
    assert credited == 994_000
    assert [c.args[0] for c in primary.request_mint.await_args_list] == [999, 994]
    foreign.melt.assert_awaited_once()
    assert foreign.melt.await_args is not None
    assert foreign.melt.await_args.kwargs["quote_id"] == "melt-994"
    primary.mint.assert_awaited_once_with(994, quote_id="mint-994")
    # Every call to the sender's mint ran with the wallet guard released.
    assert foreign.guard_depth_seen and set(foreign.guard_depth_seen) == {0}

    await session.refresh(key)
    assert key.balance == 994_000
    assert key.refund_mint_url == FOREIGN
    (row,) = await _swap_rows(session)
    assert (row.status, row.direction, row.destination_amount) == (
        "credited",
        "in",
        994,
    )
    assert row.fee_reserve == 5 and row.input_fees == 1
    ledger = (await session.exec(select(CashuTransaction))).all()
    assert [(t.type, t.amount, t.mint_url) for t in ledger] == [("in", 994, PRIMARY)]


@pytest.mark.asyncio
async def test_swap_in_accepts_proofs_from_rotated_keysets(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    foreign = _ForeignWallet(fee_reserve=1)
    primary = _PrimaryWallet()
    token = _token()
    token.keysets = ["old", "new"]
    token.proofs = [_proof(400), _proof(600)]

    async with _swap_env(foreign, primary, token):
        credited = await fms.swap_in_and_credit("cashuArotated", key, session)

    assert credited == 998_000
    foreign.get_fees_for_proofs.assert_called_once_with(token.proofs)
    assert foreign.melt.await_count == 1


@pytest.mark.asyncio
async def test_token_hash_is_unique_across_swap_journals(engine: AsyncEngine) -> None:
    first = CashuSwap(
        direction="in",
        status="failed",
        token_hash="same-token",
        source_mint=FOREIGN,
        source_unit="sat",
        source_amount=100,
        destination_mint=PRIMARY,
        destination_unit="sat",
        destination_amount=0,
    )
    second = CashuSwap(
        direction="in",
        status="melting",
        token_hash="same-token",
        source_mint=FOREIGN,
        source_unit="sat",
        source_amount=100,
        destination_mint=PRIMARY,
        destination_unit="sat",
        destination_amount=0,
    )
    await fms._save(first)
    with pytest.raises(IntegrityError):
        await fms._save(second)


@pytest.mark.asyncio
async def test_swap_in_fee_shortfall_spends_nothing(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    foreign = _ForeignWallet(fee_reserve=2000)
    primary = _PrimaryWallet()
    async with _swap_env(foreign, primary, _token(1000)):
        with pytest.raises(ForeignMintSwapError):
            await fms.swap_in_and_credit("cashuAsmall", key, session)
    foreign.melt.assert_not_awaited()
    assert await _swap_rows(session) == []
    await session.refresh(key)
    assert key.balance == 0


@pytest.mark.asyncio
async def test_swap_in_melt_timeout_is_journaled_as_ambiguous(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    foreign = _ForeignWallet(melt_state="hang")
    primary = _PrimaryWallet()
    async with _swap_env(foreign, primary, _token(1000)):
        with pytest.raises(SwapPendingError):
            await fms.swap_in_and_credit("cashuAhang", key, session)
    primary.mint.assert_not_awaited()
    (row,) = await _swap_rows(session)
    assert row.status == "ambiguous"
    assert row.melt_quote_id == "melt-994"
    await session.refresh(key)
    assert key.balance == 0


@pytest.mark.asyncio
async def test_swap_in_mint_refusal_fails_without_consuming_token(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    foreign = _ForeignWallet(
        melt_state=Exception(
            "Mint Error: not enough inputs provided for melt. Provided: 999, needed: 1004 (Code: 11000)"
        )
    )
    primary = _PrimaryWallet()
    async with _swap_env(foreign, primary, _token(1000)):
        with pytest.raises(ForeignMintSwapError):
            await fms.swap_in_and_credit("cashuArefused", key, session)
    (row,) = await _swap_rows(session)
    assert row.status == "failed"


@pytest.mark.asyncio
async def test_swap_in_rejects_replayed_token(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    foreign = _ForeignWallet()
    primary = _PrimaryWallet()
    async with _swap_env(foreign, primary, _token(1000)):
        await fms.swap_in_and_credit("cashuAonce", key, session)
        with pytest.raises(ValueError, match="already spent"):
            await fms.swap_in_and_credit("cashuAonce", key, session)
    foreign.melt.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciler_credits_ambiguous_swap_once_mint_confirms_paid(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    foreign = _ForeignWallet(melt_state="hang")
    primary = _PrimaryWallet()
    async with _swap_env(foreign, primary, _token(1000)):
        with pytest.raises(SwapPendingError):
            await fms.swap_in_and_credit("cashuAlate", key, session)
        (row,) = await _swap_rows(session)
        await fms._update(row, updated_at=int(time.time()) - 10_000)
        _mint_recovered()
        await fms.reconcile_swaps_once()

    foreign.get_melt_quote.assert_awaited_once_with("melt-994")
    primary.mint.assert_awaited_once_with(994, quote_id="mint-994")
    await session.refresh(key)
    assert key.balance == 994_000
    assert key.refund_mint_url == FOREIGN
    (row,) = await _swap_rows(session)
    assert row.status == "credited" and row.claimed_at is None


@pytest.mark.asyncio
async def test_minted_swap_credit_is_atomic_and_cannot_repeat(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    swap = CashuSwap(
        direction="in",
        status="minted",
        api_key_hashed_key=key.hashed_key,
        token="cashuAatomic",
        token_hash="atomic",
        source_mint=FOREIGN,
        source_unit="sat",
        source_amount=1000,
        destination_mint=PRIMARY,
        destination_unit="sat",
        destination_amount=998,
    )
    await fms._save(swap)

    credited = await fms._finish_swap_in(swap, key=key, session=session)
    assert credited == 998_000
    swap.status = "minted"  # stale worker copy after the first transaction
    with pytest.raises(TokenConsumedError, match="already credited"):
        await fms._finish_swap_in(swap, key=key, session=session)

    await session.refresh(key)
    assert key.balance == 998_000
    rows = list((await session.exec(select(CashuTransaction))).all())
    assert len(rows) == 1
    (stored,) = await _swap_rows(session)
    assert stored.status == "credited"


@pytest.mark.asyncio
async def test_mint_recovery_reuses_proofs_tagged_with_quote() -> None:
    proof = SimpleNamespace(amount=998, reserved=False, mint_id="mint-998")
    mint = AsyncMock(side_effect=AssertionError("must not mint the quote twice"))
    fake_wallet: Any = SimpleNamespace(
        proofs=[proof],
        load_proofs=AsyncMock(),
        available_balance=SimpleNamespace(amount=998),
        mint=mint,
    )

    recovered = await fms._mint_with_recovery(
        fake_wallet,
        998,
        "mint-998",
        mint_url=PRIMARY,
        foreign=False,
    )

    assert recovered == [proof]
    mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_fails_swap_the_mint_reports_unpaid(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    foreign = _ForeignWallet(melt_state="hang")
    foreign.get_melt_quote.return_value = SimpleNamespace(state=MeltQuoteState.unpaid)
    primary = _PrimaryWallet()
    async with _swap_env(foreign, primary, _token(1000)):
        with pytest.raises(SwapPendingError):
            await fms.swap_in_and_credit("cashuAunpaid", key, session)
        (row,) = await _swap_rows(session)
        await fms._update(row, updated_at=int(time.time()) - 10_000)
        _mint_recovered()
        await fms.reconcile_swaps_once()

    primary.mint.assert_not_awaited()
    (row,) = await _swap_rows(session)
    assert row.status == "failed"
    await session.refresh(key)
    assert key.balance == 0


@pytest.mark.asyncio
async def test_reconciler_leaves_fresh_rows_alone(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session)
    foreign = _ForeignWallet(melt_state="hang")
    primary = _PrimaryWallet()
    async with _swap_env(foreign, primary, _token(1000)):
        with pytest.raises(SwapPendingError):
            await fms.swap_in_and_credit("cashuAfresh", key, session)
        await fms.reconcile_swaps_once()
    foreign.get_melt_quote.assert_not_awaited()


# --- refund back to the user's mint -------------------------------------------


def test_refund_destination_requires_foreign_mint(engine: AsyncEngine) -> None:
    foreign_key = ApiKey(hashed_key=KEY_HASH, refund_mint_url=FOREIGN)
    assert fms.refund_destination_mint(foreign_key) == FOREIGN
    assert fms.refund_destination_mint(ApiKey(hashed_key=KEY_HASH)) is None
    assert (
        fms.refund_destination_mint(
            ApiKey(hashed_key=KEY_HASH, refund_mint_url=PRIMARY)
        )
        is None
    )


async def _open_cashu_refund(session: AsyncSession, key: ApiKey) -> Refund:
    return await refund_module.open_claim(
        session, key, method="cashu", destination=None
    )


@pytest.mark.asyncio
async def test_open_claim_records_users_mint_as_destination(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session, balance=1_000_000, refund_mint_url=FOREIGN)
    claim = await _open_cashu_refund(session, key)
    assert claim.destination == FOREIGN
    assert claim.mint_url == PRIMARY


@pytest.mark.asyncio
async def test_refund_swaps_back_to_users_mint_net_of_fees(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session, balance=1_000_000, refund_mint_url=FOREIGN)
    claim = await _open_cashu_refund(session, key)
    foreign = _ForeignWallet()
    primary = _PrimaryWallet(fee_reserve=2)
    execute = AsyncMock(return_value=(998, PRIMARY, "sat"))
    async with _swap_env(foreign, primary, _token()):
        with (
            patch.object(fms, "_execute_bolt11_payment", execute),
            patch.object(
                refund_module,
                "deserialize_token_from_string",
                return_value=SimpleNamespace(amount=998, unit="sat"),
            ),
        ):
            result = await refund_module.execute(session, claim)

    assert result["status"] == "paid"
    assert result["token"] == "cashuBrefund"
    assert result["recipient"] == FOREIGN
    assert result["sats"] == "998"
    # 1000 sat refund, 2 sat fee reserve, 0 input fees -> 998 sat on the user's mint.
    assert [c.args[0] for c in foreign.request_mint.await_args_list] == [1000, 998]
    foreign.mint.assert_awaited_once_with(998, quote_id="mint-998")
    assert execute.await_args is not None
    plan = execute.await_args.args[0]
    assert (plan.mint_url, plan.quote.quote) == (PRIMARY, "melt-998")
    assert foreign.guard_depth_seen and set(foreign.guard_depth_seen) == {0}

    refreshed = await _refund_row(session, claim.id)
    assert (refreshed.status, refreshed.token, refreshed.mint_url) == (
        "paid",
        "cashuBrefund",
        FOREIGN,
    )
    (row,) = await _swap_rows(session)
    assert (row.direction, row.status, row.destination_amount) == (
        "out",
        "settled",
        998,
    )
    assert row.refund_id == claim.id


@pytest.mark.asyncio
async def test_reconciler_settles_a_persisted_issued_refund(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session, balance=1_000_000, refund_mint_url=FOREIGN)
    claim = await _open_cashu_refund(session, key)
    swap = CashuSwap(
        direction="out",
        status="issued",
        api_key_hashed_key=key.hashed_key,
        refund_id=claim.id,
        source_mint=PRIMARY,
        source_unit="sat",
        source_amount=1000,
        destination_mint=FOREIGN,
        destination_unit="sat",
        destination_amount=998,
        token="cashuBpersisted",
    )
    await fms._save(swap)

    with patch.object(refund_module, "_record_cashu_payout", AsyncMock()) as record:
        await fms.reconcile_swaps_once()

    refreshed = await _refund_row(session, claim.id)
    assert (refreshed.status, refreshed.token) == ("paid", "cashuBpersisted")
    (stored,) = await _swap_rows(session)
    assert stored.status == "settled" and stored.claimed_at is None
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_refund_swap_ambiguous_melt_withholds_balance(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session, balance=1_000_000, refund_mint_url=FOREIGN)
    claim = await _open_cashu_refund(session, key)
    foreign = _ForeignWallet()
    primary = _PrimaryWallet(fee_reserve=2)
    execute = AsyncMock(side_effect=Bolt11PaymentAmbiguous("melt did not return"))
    async with _swap_env(foreign, primary, _token()):
        with patch.object(fms, "_execute_bolt11_payment", execute):
            with pytest.raises(Exception) as exc_info:
                await refund_module.execute(session, claim)
    assert getattr(exc_info.value, "status_code", None) == 502
    foreign.mint.assert_not_awaited()
    refreshed = await _refund_row(session, claim.id)
    assert (refreshed.status, refreshed.quote_id) == ("ambiguous", "melt-998")
    (row,) = await _swap_rows(session)
    assert row.status == "ambiguous"
    await session.refresh(key)
    assert key.balance == 0


@pytest.mark.asyncio
async def test_refund_swap_reconciler_finishes_after_melt_paid(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session, balance=1_000_000, refund_mint_url=FOREIGN)
    claim = await _open_cashu_refund(session, key)
    foreign = _ForeignWallet()
    primary = _PrimaryWallet(fee_reserve=2)
    execute = AsyncMock(side_effect=Bolt11PaymentAmbiguous("melt did not return"))
    async with _swap_env(foreign, primary, _token()):
        with patch.object(fms, "_execute_bolt11_payment", execute):
            with pytest.raises(Exception):
                await refund_module.execute(session, claim)
        (row,) = await _swap_rows(session)
        await fms._update(row, updated_at=int(time.time()) - 10_000)
        with patch.object(
            fms, "_check_bolt11_payment_status_locked", AsyncMock(return_value="paid")
        ):
            await fms.reconcile_swaps_once()

    foreign.mint.assert_awaited_once_with(998, quote_id="mint-998")
    refreshed = await _refund_row(session, claim.id)
    assert (refreshed.status, refreshed.token) == ("paid", "cashuBrefund")
    (row,) = await _swap_rows(session)
    assert row.status == "settled"


@pytest.mark.asyncio
async def test_refund_swap_reconciler_releases_balance_when_unpaid(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session, balance=1_000_000, refund_mint_url=FOREIGN)
    claim = await _open_cashu_refund(session, key)
    foreign = _ForeignWallet()
    primary = _PrimaryWallet(fee_reserve=2)
    execute = AsyncMock(side_effect=Bolt11PaymentAmbiguous("melt did not return"))
    async with _swap_env(foreign, primary, _token()):
        with patch.object(fms, "_execute_bolt11_payment", execute):
            with pytest.raises(Exception):
                await refund_module.execute(session, claim)
        (row,) = await _swap_rows(session)
        await fms._update(row, updated_at=int(time.time()) - 10_000)
        with patch.object(
            fms, "_check_bolt11_payment_status_locked", AsyncMock(return_value="unpaid")
        ):
            await fms.reconcile_swaps_once()

    foreign.mint.assert_not_awaited()
    refreshed = await _refund_row(session, claim.id)
    assert refreshed.status == "failed"
    await session.refresh(key)
    assert key.balance == 1_000_000
    (row,) = await _swap_rows(session)
    assert row.status == "failed"


@pytest.mark.asyncio
async def test_refund_reconciler_does_not_mark_swap_claims_stuck(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session, balance=1_000_000, refund_mint_url=FOREIGN)
    claim = await _open_cashu_refund(session, key)
    await refund_module.record_quote(claim, "melt-998", PRIMARY)
    await refund_module._reconcile(claim, int(time.time()) + 10_000)
    refreshed = await _refund_row(session, claim.id)
    assert refreshed.status == "pending"


@pytest.mark.asyncio
async def test_trusted_refund_mint_still_pays_directly(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    key = await _make_key(session, balance=1_000_000, refund_mint_url=PRIMARY)
    claim = await _open_cashu_refund(session, key)
    assert claim.destination is None
    send_token = AsyncMock(return_value="cashuBdirect")
    swap = AsyncMock()
    with (
        patch.object(refund_module, "send_token", send_token),
        patch.object(fms, "swap_out_for_refund", swap),
        patch.object(refund_module, "token_mint_url", lambda t, f: PRIMARY),
    ):
        result = await refund_module.execute(session, claim)
    assert result["token"] == "cashuBdirect"
    swap.assert_not_awaited()


# --- classification -----------------------------------------------------------


def _status_and_code(error: Exception) -> tuple[int, str]:
    classified = wallet.classify_redemption_error(error)
    assert classified is not None
    return classified[1], classified[3]


def test_swap_errors_have_dedicated_codes() -> None:
    assert _status_and_code(ForeignMintSwapError("x")) == (
        422,
        "cashu_foreign_mint_swap_failed",
    )
    assert _status_and_code(SwapPendingError("x")) == (409, "cashu_swap_pending")
    assert _status_and_code(ForeignMintUnavailableError("x")) == (
        503,
        "cashu_source_mint_unreachable",
    )


def test_ambiguous_melt_error_type_is_lnurl_ambiguous() -> None:
    assert issubclass(MeltOutcomeAmbiguousError, Exception)
