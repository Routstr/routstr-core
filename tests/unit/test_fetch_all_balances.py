import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.wallet import fetch_all_balances


@asynccontextmanager
async def _fake_session():  # type: ignore[no-untyped-def]
    yield MagicMock()


def _patches(  # type: ignore[no-untyped-def]
    proof_amount: int = 1000, user_balance_msats: int = 0
):
    proof = MagicMock(amount=proof_amount)
    return [
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[proof]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch(
            "routstr.wallet.db.balances_by_mint_and_unit",
            AsyncMock(
                return_value={("http://primary:3338", "sat"): user_balance_msats}
            ),
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
async def test_fetch_all_balances_closes_db_session_before_concurrent_mint_io() -> None:
    """Slow mint checks must never run while the balance DB session is open."""
    from routstr.core.settings import settings

    session_open = False
    mint_calls = 0

    @asynccontextmanager
    async def tracked_session():  # type: ignore[no-untyped-def]
        nonlocal session_open
        session_open = True
        try:
            yield MagicMock()
        finally:
            session_open = False

    async def slow_filter(proofs, wallet):  # type: ignore[no-untyped-def]
        nonlocal mint_calls
        assert session_open is False
        mint_calls += 1
        await asyncio.sleep(0)
        return proofs

    with (
        patch.object(settings, "cashu_mints", ["http://one:3338", "http://two:3338"]),
        patch.object(settings, "primary_mint", "http://one:3338"),
        patch("routstr.wallet.db.create_session", tracked_session),
        patch(
            "routstr.wallet.db.balances_by_mint_and_unit",
            AsyncMock(return_value={}),
            create=True,
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[MagicMock(amount=1)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=slow_filter),
        ),
    ):
        details, *_ = await fetch_all_balances(units=["sat", "msat"])

    assert mint_calls == 4
    assert all("error" not in detail for detail in details)


@pytest.mark.asyncio
async def test_fetch_all_balances_bounds_parallel_mint_checks() -> None:
    """A slow mint fleet cannot create an unbounded external-I/O fan-out."""
    from routstr.core.settings import settings

    active = 0
    peak = 0

    async def slow_filter(proofs, wallet):  # type: ignore[no-untyped-def]
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return proofs

    with (
        patch.object(
            settings,
            "cashu_mints",
            [f"http://mint-{index}:3338" for index in range(8)],
        ),
        patch.object(settings, "primary_mint", ""),
        patch.object(settings, "mint_operation_concurrency", 2),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch(
            "routstr.wallet.db.balances_by_mint_and_unit",
            AsyncMock(return_value={}),
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=slow_filter),
        ),
    ):
        details, *_ = await fetch_all_balances(units=["sat"])

    assert len(details) == 8
    assert peak == 2


@pytest.mark.asyncio
async def test_slow_mints_do_not_exhaust_a_single_connection_pool(
    tmp_path: Path,
) -> None:
    """Concurrent slow balance refreshes release the sole DB connection promptly."""
    from routstr.core.settings import settings

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pool-pressure.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def single_pool_session() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    async def slow_filter(proofs, wallet):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.3)
        return proofs

    try:
        with (
            patch.object(settings, "cashu_mints", ["http://slow:3338"]),
            patch.object(settings, "primary_mint", "http://slow:3338"),
            patch.object(settings, "mint_operation_concurrency", 1),
            patch("routstr.wallet.db.create_session", single_pool_session),
            patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
            patch(
                "routstr.wallet.get_proofs_per_mint_and_unit",
                MagicMock(return_value=[]),
            ),
            patch(
                "routstr.wallet.slow_filter_spend_proofs",
                AsyncMock(side_effect=slow_filter),
            ),
        ):
            results = await asyncio.gather(
                *(fetch_all_balances(units=["sat"]) for _ in range(6))
            )

        assert all("error" not in result[0][0] for result in results)
        assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_all_balances_reports_liability_when_wallet_is_empty() -> None:
    """An empty wallet must not hide outstanding user liabilities."""
    from routstr.core.settings import settings

    with (
        patch.object(settings, "cashu_mints", []),
        patch.object(settings, "primary_mint", "http://primary:3338"),
    ):
        for p in _patches(proof_amount=0, user_balance_msats=5000):
            p.start()
        try:
            details, total_wallet, total_user, owner = await fetch_all_balances(
                units=["sat"]
            )
        finally:
            patch.stopall()

    assert details[0]["wallet_balance"] == 0
    assert details[0]["user_balance"] == 5
    assert details[0]["owner_balance"] == -5
    assert total_wallet == 0
    assert total_user == 5
    assert owner == -5


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


@pytest.mark.asyncio
async def test_fetch_all_balances_degrades_when_liability_read_fails() -> None:
    from routstr.core.settings import settings

    with (
        patch.object(settings, "cashu_mints", []),
        patch.object(settings, "primary_mint", "http://primary:3338"),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch(
            "routstr.wallet.db.balances_by_mint_and_unit",
            AsyncMock(side_effect=RuntimeError("db pool exhausted")),
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=MagicMock())),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            MagicMock(return_value=[MagicMock(amount=1000)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
    ):
        details, total_wallet, total_user, owner = await fetch_all_balances(
            units=["sat"]
        )

    assert details[0]["error"] == "db pool exhausted"
    assert details[0]["wallet_balance"] == 1000
    assert details[0]["user_balance"] == 0
    assert details[0]["owner_balance"] == 0
    assert (total_wallet, total_user, owner) == (1000, 0, 0)


@pytest.mark.asyncio
async def test_liability_error_keeps_more_specific_mint_error() -> None:
    from routstr.core.settings import settings

    with (
        patch.object(settings, "cashu_mints", []),
        patch.object(settings, "primary_mint", "http://primary:3338"),
        patch("routstr.wallet.db.create_session", _fake_session),
        patch(
            "routstr.wallet.db.balances_by_mint_and_unit",
            AsyncMock(side_effect=RuntimeError("db pool exhausted")),
        ),
        patch(
            "routstr.wallet.get_wallet",
            AsyncMock(side_effect=RuntimeError("mint down")),
        ),
    ):
        details, *_ = await fetch_all_balances(units=["sat"])

    assert details[0]["error"] == "mint down"
