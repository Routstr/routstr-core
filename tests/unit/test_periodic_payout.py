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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.wallet import _payout_units, periodic_payout

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
    """primary_mint absent from cashu_mints is still paid out."""
    from routstr.core.settings import settings

    get_wallet = AsyncMock(return_value=MagicMock())
    raw_send = AsyncMock(return_value=1000)

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
        patch("routstr.wallet.raw_send_to_lnurl", raw_send),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    processed = {call.args[0] for call in get_wallet.await_args_list}
    assert processed == {"http://primary:3338"}
    assert raw_send.await_count >= 1


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
        mint_url: str, unit: str, force_reload: bool = False
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
        patch("routstr.wallet.raw_send_to_lnurl", raw_send),
    ):
        with pytest.raises(_LoopBreak):
            await periodic_payout()

    # The bad mint raised on get_wallet for both units, yet the good mint was
    # still reached and paid out for both units — failures are isolated.
    good_calls = [
        c for c in get_wallet.await_args_list if c.args[0] == "http://good:3338"
    ]
    assert len(good_calls) == 2  # sat + msat
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

    # The liability session is opened per mint/unit (sat + msat), and each
    # DB failure retains the cycle-specific alert wording while remaining
    # isolated to its own iteration.
    assert create_session.call_count == 2
    assert logger.error.call_count == 2
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
