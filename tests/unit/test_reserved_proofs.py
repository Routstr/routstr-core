"""Reserved proofs are grouped by melt quote and settled only on mint verdicts."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.reserved_proofs import inspect_reserved_proofs, reconcile_reserved_proofs

MINT = "https://mint.test"


@pytest.fixture(autouse=True)
def isolate_wallet_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "routstr.wallet._WALLET_OPERATION_LOCK", tmp_path / "wallet.lock"
    )


def _proof(
    secret: str, amount: int, *, melt_id: str | None = None, keyset: str = "ks-sat"
) -> SimpleNamespace:
    return SimpleNamespace(
        id=keyset,
        secret=secret,
        amount=amount,
        reserved=True,
        melt_id=melt_id,
        send_id=None,
        time_reserved="2026-09-20 10:00:00",
    )


def _quote(quote: str, state: str, amount: int, fee: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        quote=quote,
        state=SimpleNamespace(value=state),
        amount=amount,
        fee_reserve=fee,
        created_time=1_758_000_000,
        request="lnbc1test",
        mint=MINT,
        unit="sat",
    )


_KEYSETS = [
    SimpleNamespace(id="ks-sat", mint_url=MINT, unit=SimpleNamespace(name="sat"))
]


@contextmanager
def _wallet_db(
    proofs: list[SimpleNamespace], quotes: list[SimpleNamespace]
) -> Iterator[MagicMock]:
    wallet = MagicMock()
    wallet.db = object()
    wallet.invalidate = AsyncMock()
    with ExitStack() as stack:
        for target in (
            patch("routstr.reserved_proofs.get_wallet", AsyncMock(return_value=wallet)),
            patch(
                "routstr.reserved_proofs.get_keysets", AsyncMock(return_value=_KEYSETS)
            ),
            patch(
                "routstr.reserved_proofs.get_reserved_proofs",
                AsyncMock(return_value=proofs),
            ),
            patch(
                "routstr.reserved_proofs.get_bolt11_melt_quotes",
                AsyncMock(return_value=quotes),
            ),
        ):
            stack.enter_context(target)
        yield wallet


@pytest.mark.asyncio
async def test_inspect_groups_by_quote_and_flags_unquoted() -> None:
    proofs = [
        _proof("a", 64, melt_id="q-pending"),
        _proof("b", 8, melt_id="q-pending"),
        _proof("c", 16, melt_id="q-unpaid"),
        _proof("d", 4),
    ]
    quotes = [_quote("q-pending", "PENDING", 70), _quote("q-unpaid", "UNPAID", 15)]
    with _wallet_db(proofs, quotes):
        result = await inspect_reserved_proofs()

    by_key = {g["key"]: g for g in result["groups"]}
    assert by_key["q-pending"]["proof_amount"] == 72
    assert by_key["q-pending"]["proof_count"] == 2
    assert by_key["q-pending"]["hint"] == "check_mint"
    assert by_key["q-unpaid"]["hint"] == "releasable"
    unquoted = by_key[f"{MINT}|sat|unquoted"]
    assert unquoted["kind"] == "unquoted"
    assert unquoted["proof_amount"] == 4
    assert result["totals"]["reserved_sat"] == 92
    assert result["totals"]["releasable_sat"] == 16


@pytest.mark.asyncio
async def test_reconcile_follows_mint_verdict_per_quote() -> None:
    proofs = [
        _proof("a", 64, melt_id="q-paid"),
        _proof("b", 16, melt_id="q-unpaid"),
        _proof("c", 32, melt_id="q-pending"),
    ]
    quotes = [
        _quote("q-paid", "PENDING", 62),
        _quote("q-unpaid", "PENDING", 14),
        _quote("q-pending", "PENDING", 30),
    ]
    verdicts = {"q-paid": "paid", "q-unpaid": "unpaid", "q-pending": "pending"}

    async def status(_mint: str, _unit: str, quote_id: str) -> str:
        return verdicts[quote_id]

    with (
        _wallet_db(proofs, quotes) as wallet,
        patch("routstr.reserved_proofs._check_bolt11_payment_status_locked", status),
        # After the paid verdict the mint reports the leftover proof as spent.
        patch(
            "routstr.reserved_proofs.filter_unspent_proofs", AsyncMock(return_value=[])
        ),
    ):
        outcome = await reconcile_reserved_proofs()

    actions = {r["key"]: r for r in outcome["results"]}
    assert actions["q-unpaid"]["action"] == "released"
    assert actions["q-unpaid"]["released_amount"] == 16
    assert actions["q-paid"]["action"] == "settled_paid"
    assert actions["q-paid"]["pruned_amount"] == 64
    assert actions["q-pending"]["action"] == "left_reserved"
    assert actions["q-pending"]["released_amount"] == 0
    wallet.invalidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_keeps_unspent_unquoted_proofs_reserved() -> None:
    spent = _proof("spent", 8)
    outstanding = _proof("token", 32)
    with (
        _wallet_db([spent, outstanding], []) as wallet,
        patch(
            "routstr.reserved_proofs.filter_unspent_proofs",
            AsyncMock(return_value=[outstanding]),
        ),
    ):
        outcome = await reconcile_reserved_proofs()

    (result,) = outcome["results"]
    assert result["action"] == "checked"
    assert result["pruned_amount"] == 8
    assert result["outstanding_amount"] == 32
    wallet.invalidate.assert_awaited_once_with([spent])


@pytest.mark.asyncio
async def test_reconcile_isolates_mint_failures() -> None:
    with (
        _wallet_db([_proof("a", 8, melt_id="q-1")], [_quote("q-1", "PENDING", 6)]),
        patch(
            "routstr.reserved_proofs._check_bolt11_payment_status_locked",
            AsyncMock(side_effect=RuntimeError("mint down")),
        ),
    ):
        outcome = await reconcile_reserved_proofs()

    (result,) = outcome["results"]
    assert result["action"] == "error"
    assert result["error"] == "mint down"
