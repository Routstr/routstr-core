"""Tests for periodic_payout() resilience fixes.

Covers two regressions from the auto-payout / primary-mint audit
(docs/auto-payout-primary-mint-failure-report.md):

1. periodic_payout() must include settings.primary_mint even when it is not
   listed in settings.cashu_mints, matching fetch_all_balances(); otherwise
   primary-mint funds never auto-payout.
2. A failure on one mint/unit must not abort payout for the remaining
   mint/units in the same cycle (the try/except is now per mint/unit).
"""

from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

from routstr.payment.lnurl import MeltOutcomeAmbiguousError, MeltUnpaidError
from routstr.wallet import (
    _payout_units,
    _reconcile_stale_payout_history,
    periodic_payout,
)


@pytest.fixture(autouse=True)
def isolate_wallet_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "routstr.wallet._WALLET_OPERATION_LOCK", tmp_path / "wallet.lock"
    )


# Sentinel interval used to break the otherwise-infinite payout loop after
# exactly one full cycle.
_INTERVAL = 987


class _LoopBreak(Exception):
    """Raised via the patched sleep to stop periodic_payout after one cycle."""


@asynccontextmanager
async def _fake_session():  # type: ignore[no-untyped-def]
    yield MagicMock()


def _one_cycle_sleep() -> Callable[[float], Coroutine[Any, Any, None]]:
    """Return an async sleep stub that lets exactly one payout cycle run.

    The top-of-loop sleep uses the sentinel interval; the second time it is
    seen (start of the second cycle) we raise to break out. The inner
    ``asyncio.sleep(5)`` pass-through is ignored.
    """
    seen = {"interval": 0}

    async def _sleep(seconds: float) -> None:
        if seconds == _INTERVAL:
            seen["interval"] += 1
            if seen["interval"] >= 2:
                raise _LoopBreak()

    return _sleep


@pytest.mark.asyncio
async def test_periodic_payout_includes_primary_mint_not_in_cashu_mints() -> None:
    """primary_mint absent from cashu_mints is paid out and recorded."""
    from routstr.core.settings import settings

    get_wallet = AsyncMock(return_value=MagicMock())
    record_payout = AsyncMock()
    settle_payout = AsyncMock()

    async def send(*args: object, **kwargs: object) -> int:
        await kwargs["on_melt_quote"](  # type: ignore[index,operator]
            "quote-1", "lnbc1payout"
        )
        return 1_000_000

    raw_send = AsyncMock(side_effect=send)

    with (
        patch.object(settings, "cashu_mints", []),
        patch.object(settings, "primary_mint", "http://primary:3338"),
        patch.object(settings, "receive_ln_address", "owner@ln.tld"),
        patch.object(settings, "payout_interval_seconds", _INTERVAL),
        patch.object(settings, "min_payout_sat", 10),
        patch("routstr.wallet.asyncio.sleep", _one_cycle_sleep()),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch(
            "routstr.wallet._get_supported_mint_units",
            AsyncMock(return_value=["sat"]),
        ),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[MagicMock(amount=100_000)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch(
            "routstr.wallet.db.total_user_liability",
            AsyncMock(return_value=0),
        ),
        patch(
            "routstr.wallet.db.user_liability_for_mint_and_unit",
            AsyncMock(return_value=0),
        ),
        patch("routstr.wallet.db.record_lightning_payout", record_payout),
        patch("routstr.wallet.db.settle_lightning_payout", settle_payout),
        patch("routstr.wallet.raw_send_to_lnurl", raw_send),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    processed = {call.args[0] for call in get_wallet.await_args_list}
    assert processed == {"http://primary:3338"}
    assert raw_send.await_count >= 1
    record_payout.assert_awaited_once_with(
        ANY,
        quote_id="quote-1",
        bolt11="lnbc1payout",
        amount_sats=100_000,
        mint_url="http://primary:3338",
        destination="owner@ln.tld",
    )
    settle_payout.assert_awaited_once_with(
        ANY,
        "quote-1",
        status="paid",
        amount_sats=1_000,
    )


@pytest.mark.asyncio
async def test_periodic_payout_releases_session_before_slow_mint_send() -> None:
    """The DB connection is returned before the external LNURL call starts."""
    from routstr.core.settings import settings

    session_open = False
    sends_completed = 0

    @asynccontextmanager
    async def tracked_session():  # type: ignore[no-untyped-def]
        nonlocal session_open
        session_open = True
        try:
            yield MagicMock()
        finally:
            session_open = False

    async def raw_send(*args: object, **kwargs: object) -> int:
        nonlocal sends_completed
        assert session_open is False
        sends_completed += 1
        return 1000

    with (
        patch.object(settings, "cashu_mints", []),
        patch.object(settings, "primary_mint", "http://primary:3338"),
        patch.object(settings, "receive_ln_address", "owner@ln.tld"),
        patch.object(settings, "payout_interval_seconds", _INTERVAL),
        patch.object(settings, "min_payout_sat", 10),
        patch("routstr.wallet.asyncio.sleep", _one_cycle_sleep()),
        patch("routstr.wallet.db.create_session", tracked_session),
        patch(
            "routstr.wallet._get_supported_mint_units",
            AsyncMock(return_value=["sat"]),
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[MagicMock(amount=100_000)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch(
            "routstr.wallet.db.total_user_liability",
            AsyncMock(return_value=0),
        ),
        patch(
            "routstr.wallet.db.user_liability_for_mint_and_unit",
            AsyncMock(return_value=0),
        ),
        patch("routstr.wallet.raw_send_to_lnurl", AsyncMock(side_effect=raw_send)),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    assert sends_completed == 1


@pytest.mark.asyncio
async def test_periodic_payout_isolates_failing_mint() -> None:
    """A failing mint does not prevent payout for the other mints."""
    from routstr.core.settings import settings

    async def _get_wallet(
        mint_url: str, unit: str, force_reload_proofs: bool = False
    ) -> MagicMock:
        if mint_url == "http://bad:3338":
            raise RuntimeError("mint unreachable")
        return MagicMock()

    get_wallet = AsyncMock(side_effect=_get_wallet)
    raw_send = AsyncMock(return_value=1000)

    with (
        patch.object(settings, "cashu_mints", ["http://bad:3338", "http://good:3338"]),
        patch.object(settings, "primary_mint", "http://good:3338"),
        patch.object(settings, "receive_ln_address", "owner@ln.tld"),
        patch.object(settings, "payout_interval_seconds", _INTERVAL),
        patch.object(settings, "min_payout_sat", 10),
        patch("routstr.wallet.asyncio.sleep", _one_cycle_sleep()),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch(
            "routstr.wallet._get_supported_mint_units",
            AsyncMock(return_value=["sat", "msat"]),
        ),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[MagicMock(amount=100_000)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch(
            "routstr.wallet.db.total_user_liability",
            AsyncMock(return_value=0),
        ),
        patch(
            "routstr.wallet.db.user_liability_for_mint_and_unit",
            AsyncMock(return_value=0),
        ),
        patch("routstr.wallet.raw_send_to_lnurl", raw_send),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    # The bad mint raised on get_wallet for both units, yet the good mint was
    # still reached and paid out for both units — failures are isolated.
    for unit in ("sat", "msat"):
        assert (
            call("http://good:3338", unit, force_reload_proofs=True)
            in get_wallet.await_args_list
        )
    assert raw_send.await_count == 2  # good mint paid for both units


@pytest.mark.asyncio
async def test_periodic_payout_handles_session_creation_failure() -> None:
    """A db.create_session failure is logged per mint/unit and the loop continues."""
    from routstr.core.settings import settings

    create_session = MagicMock(side_effect=RuntimeError("db unavailable"))
    logger = MagicMock()

    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch.object(settings, "primary_mint", "http://mint:3338"),
        patch.object(settings, "receive_ln_address", "owner@ln.tld"),
        patch.object(settings, "payout_interval_seconds", _INTERVAL),
        patch("routstr.wallet.asyncio.sleep", _one_cycle_sleep()),
        patch("routstr.wallet.db.create_session", create_session),
        patch.object(settings, "min_payout_sat", 10),
        patch(
            "routstr.wallet._get_supported_mint_units",
            AsyncMock(return_value=["sat", "msat"]),
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[MagicMock(amount=100_000)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch("routstr.wallet.logger", logger),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    # Per mint/unit (sat + msat) a session is opened twice: once by the stale
    # payout-history sweep and once for the liability read. Each DB failure is
    # logged and isolated to its own step; the liability error keeps the
    # cycle-specific alert wording.
    assert create_session.call_count == 4
    assert logger.error.call_count == 4
    message = logger.error.call_args.args[0]
    extra = logger.error.call_args.kwargs["extra"]
    assert message == "Error in periodic payout cycle: RuntimeError"
    assert extra["error"] == "db unavailable"


@pytest.mark.asyncio
async def test_payout_units_excludes_units_the_sender_cannot_pay() -> None:
    with patch(
        "routstr.wallet._get_supported_mint_units",
        AsyncMock(return_value=["usd", "sat", "eur", "msat"]),
    ):
        assert await _payout_units("http://mint:3338") == ["sat", "msat"]


@pytest.mark.asyncio
async def test_periodic_payout_caps_amount_at_max_payout_sat() -> None:
    """Available balance above max_payout_sat is capped for a single payout."""
    from routstr.core.settings import settings

    raw_send = AsyncMock(return_value=1000)

    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch.object(settings, "primary_mint", "http://mint:3338"),
        patch.object(settings, "receive_ln_address", "owner@ln.tld"),
        patch.object(settings, "payout_interval_seconds", _INTERVAL),
        patch.object(settings, "min_payout_sat", 10),
        patch.object(settings, "max_payout_sat", 250_000),
        patch("routstr.wallet.asyncio.sleep", _one_cycle_sleep()),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch(
            "routstr.wallet._get_supported_mint_units",
            AsyncMock(return_value=["sat"]),
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[MagicMock(amount=1_000_000)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch(
            "routstr.wallet.db.total_user_liability",
            AsyncMock(return_value=0),
        ),
        patch(
            "routstr.wallet.db.user_liability_for_mint_and_unit",
            AsyncMock(return_value=0),
        ),
        patch("routstr.wallet.raw_send_to_lnurl", raw_send),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    assert raw_send.await_count >= 1
    assert raw_send.await_args_list[0].kwargs["amount"] == 250_000


@pytest.mark.asyncio
async def test_payout_history_records_the_capped_amount() -> None:
    """History stores what is actually sent, not the uncapped balance."""
    from routstr.core.settings import settings

    record_payout = AsyncMock()
    settle_payout = AsyncMock()

    async def send(*args: object, **kwargs: object) -> int:
        await kwargs["on_melt_quote"](  # type: ignore[index,operator]
            "quote-capped", "lnbc1capped"
        )
        return 250_000_000

    raw_send = AsyncMock(side_effect=send)

    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch.object(settings, "primary_mint", "http://mint:3338"),
        patch.object(settings, "receive_ln_address", "owner@ln.tld"),
        patch.object(settings, "payout_interval_seconds", _INTERVAL),
        patch.object(settings, "min_payout_sat", 10),
        patch.object(settings, "max_payout_sat", 250_000),
        patch("routstr.wallet.asyncio.sleep", _one_cycle_sleep()),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch(
            "routstr.wallet._get_supported_mint_units",
            AsyncMock(return_value=["sat"]),
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[MagicMock(amount=1_000_000)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch("routstr.wallet.db.total_user_liability", AsyncMock(return_value=0)),
        patch(
            "routstr.wallet.db.user_liability_for_mint_and_unit",
            AsyncMock(return_value=0),
        ),
        patch(
            "routstr.wallet.db.list_unsettled_lightning_payouts",
            AsyncMock(return_value=[]),
        ),
        patch("routstr.wallet.db.record_lightning_payout", record_payout),
        patch("routstr.wallet.db.settle_lightning_payout", settle_payout),
        patch("routstr.wallet.raw_send_to_lnurl", raw_send),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    record_payout.assert_awaited_once_with(
        ANY,
        quote_id="quote-capped",
        bolt11="lnbc1capped",
        amount_sats=250_000,
        mint_url="http://mint:3338",
        destination="owner@ln.tld",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (MeltUnpaidError("mint confirmed unpaid"), "failed"),
        (MeltOutcomeAmbiguousError("outcome unknown"), "reconciliation_required"),
        (RuntimeError("HTTP 500 after dispatch"), "reconciliation_required"),
    ],
)
async def test_payout_history_marks_failed_only_on_proven_non_payment(
    error: Exception, expected_status: str
) -> None:
    """Only a mint-confirmed unpaid melt is recorded as failed."""
    from routstr.core.settings import settings

    settle_payout = AsyncMock()

    async def send(*args: object, **kwargs: object) -> int:
        await kwargs["on_melt_quote"](  # type: ignore[index,operator]
            "quote-err", "lnbc1err"
        )
        raise error

    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch.object(settings, "primary_mint", "http://mint:3338"),
        patch.object(settings, "receive_ln_address", "owner@ln.tld"),
        patch.object(settings, "payout_interval_seconds", _INTERVAL),
        patch.object(settings, "min_payout_sat", 10),
        patch.object(settings, "max_payout_sat", 250_000),
        patch("routstr.wallet.asyncio.sleep", _one_cycle_sleep()),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch(
            "routstr.wallet._get_supported_mint_units",
            AsyncMock(return_value=["sat"]),
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[MagicMock(amount=1_000_000)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch("routstr.wallet.db.total_user_liability", AsyncMock(return_value=0)),
        patch(
            "routstr.wallet.db.user_liability_for_mint_and_unit",
            AsyncMock(return_value=0),
        ),
        patch(
            "routstr.wallet.db.list_unsettled_lightning_payouts",
            AsyncMock(return_value=[]),
        ),
        patch("routstr.wallet.db.record_lightning_payout", AsyncMock()),
        patch("routstr.wallet.db.settle_lightning_payout", settle_payout),
        patch("routstr.wallet.raw_send_to_lnurl", AsyncMock(side_effect=send)),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    settle_payout.assert_awaited_once_with(
        ANY, "quote-err", status=expected_status, amount_sats=None
    )


@pytest.mark.asyncio
async def test_payout_history_write_failure_does_not_block_payout() -> None:
    """A failing history insert is logged; the melt and settlement still run."""
    from routstr.core.settings import settings

    get_wallet = AsyncMock(return_value=MagicMock())
    record_payout = AsyncMock(side_effect=RuntimeError("database is locked"))
    settle_payout = AsyncMock()
    logger = MagicMock()

    async def send(*args: object, **kwargs: object) -> int:
        await kwargs["on_melt_quote"](  # type: ignore[index,operator]
            "quote-1", "lnbc1payout"
        )
        return 1_000_000

    raw_send = AsyncMock(side_effect=send)

    with (
        patch.object(settings, "cashu_mints", []),
        patch.object(settings, "primary_mint", "http://primary:3338"),
        patch.object(settings, "receive_ln_address", "owner@ln.tld"),
        patch.object(settings, "payout_interval_seconds", _INTERVAL),
        patch.object(settings, "min_payout_sat", 10),
        patch.object(settings, "max_payout_sat", 250_000),
        patch("routstr.wallet.asyncio.sleep", _one_cycle_sleep()),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch(
            "routstr.wallet._get_supported_mint_units",
            AsyncMock(return_value=["sat"]),
        ),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[MagicMock(amount=100_000)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch("routstr.wallet.db.total_user_liability", AsyncMock(return_value=0)),
        patch(
            "routstr.wallet.db.user_liability_for_mint_and_unit",
            AsyncMock(return_value=0),
        ),
        patch(
            "routstr.wallet.db.list_unsettled_lightning_payouts",
            AsyncMock(return_value=[]),
        ),
        patch("routstr.wallet.db.record_lightning_payout", record_payout),
        patch("routstr.wallet.db.settle_lightning_payout", settle_payout),
        patch("routstr.wallet.raw_send_to_lnurl", raw_send),
        patch("routstr.wallet.logger", logger),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    record_payout.assert_awaited_once()
    assert raw_send.await_count == 1
    settle_payout.assert_awaited_once_with(
        ANY, "quote-1", status="paid", amount_sats=1_000
    )
    messages = [call.args[0] for call in logger.error.call_args_list]
    assert "Failed to record Lightning payout history" in messages


@pytest.mark.asyncio
async def test_stale_payout_history_is_reconciled_from_mint_state() -> None:
    """Stale out-rows follow the mint's verdict; pending/unknown are left alone."""
    stale = [
        MagicMock(payment_hash="q-paid"),
        MagicMock(payment_hash="q-unpaid"),
        MagicMock(payment_hash="q-pending"),
        MagicMock(payment_hash="q-unknown"),
    ]
    states = {
        "q-paid": "paid",
        "q-unpaid": "unpaid",
        "q-pending": "pending",
        "q-unknown": "unknown",
    }
    settle_payout = AsyncMock()

    async def _state(_mint: str, _unit: str, quote_id: str) -> str:
        return states[quote_id]

    with (
        patch("routstr.wallet.db.create_session", _fake_session),
        patch(
            "routstr.wallet.db.list_unsettled_lightning_payouts",
            AsyncMock(return_value=stale),
        ),
        patch("routstr.wallet._check_bolt11_payment_status_locked", _state),
        patch("routstr.wallet.db.settle_lightning_payout", settle_payout),
    ):
        await _reconcile_stale_payout_history("http://mint:3338", "sat")

    assert settle_payout.await_args_list == [
        ((ANY, "q-paid"), {"status": "paid", "amount_sats": None}),
        ((ANY, "q-unpaid"), {"status": "failed", "amount_sats": None}),
    ]
