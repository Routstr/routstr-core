from collections.abc import Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from routstr.wallet import fetch_all_balances


@pytest.fixture(autouse=True)
def clear_balance_fetch_state() -> Generator[None, None, None]:
    from routstr import wallet

    wallet._balance_fetch_failures.clear()
    wallet._balance_fetch_locks.clear()
    wallet._mint_supported_units.clear()
    wallet._MintRateGuard._guards.clear()
    yield
    wallet._balance_fetch_failures.clear()
    wallet._balance_fetch_locks.clear()
    wallet._mint_supported_units.clear()
    wallet._MintRateGuard._guards.clear()


@asynccontextmanager
async def _fake_session():  # type: ignore[no-untyped-def]
    yield MagicMock()


def _patches(proof_amount: int = 1000):  # type: ignore[no-untyped-def]
    proof = MagicMock(amount=proof_amount)
    return [
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[proof]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet, **kwargs: proofs),
        ),
        patch(
            "routstr.wallet.db.balances_for_mint_and_unit",
            AsyncMock(return_value=0),
        ),
        patch("routstr.wallet.db.create_session", _fake_session),
    ]


@pytest.mark.asyncio
async def test_fetch_all_balances_falls_back_to_primary_mint() -> None:
    """With empty cashu_mints, balances are still fetched for primary_mint."""
    from routstr.core.settings import settings

    with (
        patch.object(settings, "cashu_mints", []),
        patch.object(settings, "primary_mint", "http://primary:3338"),
    ):
        for p in _patches(proof_amount=1000):
            p.start()
        try:
            details, total_wallet, total_user, owner = await fetch_all_balances(
                units=["sat"]
            )
        finally:
            patch.stopall()

    assert [d["mint_url"] for d in details] == ["http://primary:3338"]
    assert total_wallet == 1000


@pytest.mark.asyncio
async def test_fetch_all_balances_uses_units_advertised_by_mint() -> None:
    from routstr.core.settings import settings

    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch.object(settings, "primary_mint", "http://mint:3338"),
        patch(
            "routstr.wallet._get_supported_mint_units",
            AsyncMock(return_value=["sat"]),
        ) as supported_units,
    ):
        for p in _patches(proof_amount=1000):
            p.start()
        try:
            details, *_ = await fetch_all_balances()
        finally:
            patch.stopall()

    supported_units.assert_awaited_once_with("http://mint:3338")
    assert [detail["unit"] for detail in details] == ["sat"]


@pytest.mark.asyncio
async def test_unit_discovery_failure_returns_structured_balance_error() -> None:
    from routstr.core.settings import settings

    get_wallet = AsyncMock()
    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch.object(settings, "primary_mint", "http://mint:3338"),
        patch(
            "routstr.wallet._get_supported_mint_units",
            AsyncMock(side_effect=httpx.ConnectError("mint unavailable")),
        ),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch("routstr.wallet.db.create_session", _fake_session),
    ):
        details, *_ = await fetch_all_balances()

    assert details[0]["unit"] == settings.primary_mint_unit
    assert details[0]["error_code"] == "unreachable"
    assert details[0]["retry_after_seconds"] > 0
    get_wallet.assert_not_awaited()


@pytest.mark.asyncio
async def test_supported_mint_units_come_from_active_keysets() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import _get_supported_mint_units

    sat = MagicMock(active=True)
    sat.unit.name = "sat"
    msat = MagicMock(active=False)
    msat.unit.name = "msat"
    usd = MagicMock(active=True)
    usd.unit.name = "usd"
    wallet = MagicMock()
    wallet._get_keysets = AsyncMock(return_value=[usd, msat, sat])

    with (
        patch.object(settings, "primary_mint_unit", "sat"),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=wallet)),
    ):
        units = await _get_supported_mint_units("http://mint:3338")
        cached_units = await _get_supported_mint_units("http://mint:3338")

    assert units == ["sat", "usd"]
    assert cached_units == units
    wallet._get_keysets.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_all_balances_backs_off_after_connection_failure() -> None:
    from routstr.core.settings import settings

    get_wallet = AsyncMock(side_effect=httpx.ConnectError("mint unavailable"))
    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch.object(settings, "primary_mint", "http://mint:3338"),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch("routstr.wallet.time.monotonic", return_value=10),
        patch("routstr.wallet.logger.warning") as warning,
    ):
        first = await fetch_all_balances(units=["sat"])
        second = await fetch_all_balances(units=["sat"])

    assert first[0][0]["error"] == "mint unavailable"
    assert first[0][0]["error_code"] == "unreachable"
    assert first[0][0]["retry_after_seconds"] == 60
    assert second[0][0]["error"] == "mint unavailable"
    assert second[0][0]["error_code"] == "unreachable"
    assert get_wallet.await_count == 1
    warning.assert_called_once()

    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch.object(settings, "primary_mint", "http://mint:3338"),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch("routstr.wallet.time.monotonic", return_value=71),
        patch("routstr.wallet.logger.warning"),
    ):
        await fetch_all_balances(units=["sat"])

    assert get_wallet.await_count == 2


@pytest.mark.asyncio
async def test_fetch_all_balances_reports_rate_limit_status() -> None:
    from routstr.core.settings import settings

    request = httpx.Request("GET", "http://mint:3338/v1/keysets")
    response = httpx.Response(429, request=request, headers={"Retry-After": "45"})
    error = httpx.HTTPStatusError("rate limited", request=request, response=response)
    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch.object(settings, "primary_mint", "http://mint:3338"),
        patch("routstr.wallet.get_wallet", AsyncMock(side_effect=error)),
        patch("routstr.wallet.db.create_session", _fake_session),
    ):
        details, *_ = await fetch_all_balances(units=["sat"])

    assert details[0]["error_code"] == "rate_limited"
    assert details[0]["retry_after_seconds"] == 60


@pytest.mark.asyncio
async def test_balance_failure_applies_mint_cooldown_to_other_units() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import _mint_cooldown_remaining

    mint = "http://mint:3338"
    get_wallet = AsyncMock(side_effect=httpx.ConnectError("mint unavailable"))
    with (
        patch.object(settings, "cashu_mints", [mint]),
        patch.object(settings, "primary_mint", mint),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch("routstr.wallet.time.monotonic", return_value=10),
        patch("routstr.wallet.logger.warning") as warning,
    ):
        details, *_ = await fetch_all_balances(units=["sat", "msat"])
        cooldown = _mint_cooldown_remaining(mint)

    assert get_wallet.await_count == 1
    assert warning.call_count == 1
    assert cooldown == 60
    assert details[0]["error"] == "mint unavailable"
    assert details[0]["error_code"] == "unreachable"
    assert details[1]["error"] == "Mint is unreachable"
    assert details[1]["error_code"] == "unreachable"


@pytest.mark.asyncio
async def test_fetch_all_balances_no_duplicate_primary_mint() -> None:
    """primary_mint already in cashu_mints is not inspected twice."""
    from routstr.core.settings import settings

    with (
        patch.object(settings, "cashu_mints", ["http://primary:3338"]),
        patch.object(settings, "primary_mint", "http://primary:3338"),
    ):
        for p in _patches(proof_amount=1000):
            p.start()
        try:
            details, total_wallet, _total_user, _owner = await fetch_all_balances(
                units=["sat"]
            )
        finally:
            patch.stopall()

    assert [d["mint_url"] for d in details] == ["http://primary:3338"]
    assert total_wallet == 1000
