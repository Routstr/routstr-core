import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr import wallet
from routstr.core.db import CashuTransaction
from routstr.wallet import refund_sweep_once


@pytest.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'refunds.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert(
    factory: async_sessionmaker[AsyncSession], *rows: CashuTransaction
) -> None:
    async with factory() as session:
        session.add_all(rows)
        await session.commit()


async def _load(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, CashuTransaction]:
    async with factory() as session:
        result = await session.exec(select(CashuTransaction))
        return {row.token: row for row in result.all()}


@pytest.mark.asyncio
async def test_refund_sweep_releases_db_session_during_token_redemption(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert(
        session_factory,
        CashuTransaction(
            token="eligible", amount=1, unit="sat", type="out", created_at=800
        ),
    )
    session_open = False

    @asynccontextmanager
    async def tracked_session() -> AsyncIterator[AsyncSession]:
        nonlocal session_open
        async with session_factory() as session:
            session_open = True
            try:
                yield session
            finally:
                session_open = False

    async def receive_token(token: str) -> None:
        assert token == "eligible"
        assert session_open is False

    with (
        patch("routstr.wallet.db.create_session", tracked_session),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.time.time", return_value=1000),
        patch("routstr.wallet.recieve_token", AsyncMock(side_effect=receive_token)),
    ):
        await refund_sweep_once()

    assert (await _load(session_factory))["eligible"].swept is True


@pytest.mark.asyncio
async def test_refund_sweep_only_processes_expired_eligible_outgoing_tokens(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rows = [
        CashuTransaction(
            token="eligible", amount=1, unit="sat", type="out", created_at=800
        ),
        CashuTransaction(
            token="fresh", amount=1, unit="sat", type="out", created_at=950
        ),
        CashuTransaction(
            token="boundary", amount=1, unit="sat", type="out", created_at=900
        ),
        CashuTransaction(
            token="collected",
            amount=1,
            unit="sat",
            type="out",
            created_at=800,
            collected=True,
        ),
        CashuTransaction(
            token="swept", amount=1, unit="sat", type="out", created_at=800, swept=True
        ),
        CashuTransaction(
            token="incoming", amount=1, unit="sat", type="in", created_at=800
        ),
    ]
    await _insert(session_factory, *rows)
    receive = AsyncMock()
    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.time.time", return_value=1000),
        patch("routstr.wallet.recieve_token", receive),
    ):
        await refund_sweep_once()

    receive.assert_awaited_once_with("eligible")
    loaded = await _load(session_factory)
    assert loaded["eligible"].swept is True
    assert loaded["fresh"].swept is False
    assert loaded["boundary"].swept is False
    assert loaded["collected"].swept is False
    assert loaded["swept"].swept is True
    assert loaded["incoming"].swept is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "collected", "claim_started_at"),
    [
        (RuntimeError("token already spent"), True, None),
        (RuntimeError("mint unavailable"), False, 1000),
    ],
)
async def test_refund_sweep_records_spent_and_unknown_outcomes_safely(
    session_factory: async_sessionmaker[AsyncSession],
    error: Exception,
    collected: bool,
    claim_started_at: int | None,
) -> None:
    await _insert(
        session_factory,
        CashuTransaction(
            token="refund", amount=1, unit="sat", type="out", created_at=800
        ),
    )
    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.time.time", return_value=1000),
        patch("routstr.wallet.recieve_token", AsyncMock(side_effect=error)),
    ):
        await refund_sweep_once()

    refund = (await _load(session_factory))["refund"]
    assert refund.collected is collected
    assert refund.swept is False
    assert refund.sweep_started_at == claim_started_at


@pytest.mark.asyncio
async def test_post_spend_failure_retains_claim_and_stale_retry_records_sweep(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert(
        session_factory,
        CashuTransaction(
            token="post-spend-failure",
            amount=1,
            unit="sat",
            type="out",
            created_at=800,
        ),
    )
    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.settings.refund_sweep_claim_timeout_seconds", 200),
        patch("routstr.wallet.time.time", return_value=1000),
        patch(
            "routstr.wallet.recieve_token",
            AsyncMock(
                side_effect=wallet.TokenConsumedError(
                    "Mint on primary failed after successful melt"
                )
            ),
        ),
    ):
        await refund_sweep_once()

    retained = (await _load(session_factory))["post-spend-failure"]
    assert retained.swept is False
    assert retained.collected is False
    assert retained.sweep_started_at == 1000

    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.settings.refund_sweep_claim_timeout_seconds", 200),
        patch("routstr.wallet.time.time", return_value=1300),
        patch(
            "routstr.wallet.recieve_token",
            AsyncMock(side_effect=RuntimeError("token already spent")),
        ),
    ):
        await refund_sweep_once()

    recovered = (await _load(session_factory))["post-spend-failure"]
    assert recovered.swept is True
    assert recovered.collected is False
    assert recovered.sweep_started_at is None


@pytest.mark.asyncio
async def test_refund_sweep_retains_claim_on_cancellation_during_redemption(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert(
        session_factory,
        CashuTransaction(
            token="cancelled", amount=1, unit="sat", type="out", created_at=800
        ),
    )
    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.time.time", return_value=1000),
        patch(
            "routstr.wallet.recieve_token",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await refund_sweep_once()

    refund = (await _load(session_factory))["cancelled"]
    assert refund.swept is False
    assert refund.sweep_started_at == 1000


@pytest.mark.asyncio
async def test_checkpoint_failure_retains_claim_and_stale_retry_records_sweep(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert(
        session_factory,
        CashuTransaction(
            token="checkpoint-failure",
            amount=1,
            unit="sat",
            type="out",
            created_at=800,
        ),
    )
    real_set_state = wallet._set_refund_sweep_state

    async def fail_swept_checkpoint(
        refund_id: str,
        *,
        predicates: tuple[object, ...] = (),
        **values: object,
    ) -> int:
        if values.get("swept") is True:
            raise RuntimeError("checkpoint unavailable")
        return await real_set_state(refund_id, predicates=predicates, **values)

    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.settings.refund_sweep_claim_timeout_seconds", 200),
        patch("routstr.wallet.time.time", return_value=1000),
        patch(
            "routstr.wallet.recieve_token", AsyncMock(return_value=(1, "sat", "mint"))
        ),
        patch(
            "routstr.wallet._set_refund_sweep_state",
            side_effect=fail_swept_checkpoint,
        ),
        patch("routstr.wallet.logger.critical") as critical,
    ):
        await refund_sweep_once()

    retained = (await _load(session_factory))["checkpoint-failure"]
    assert retained.swept is False
    assert retained.collected is False
    assert retained.sweep_started_at == 1000
    critical.assert_called_once()

    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.settings.refund_sweep_claim_timeout_seconds", 200),
        patch("routstr.wallet.time.time", return_value=1300),
        patch(
            "routstr.wallet.recieve_token",
            AsyncMock(side_effect=RuntimeError("token already spent")),
        ),
    ):
        await refund_sweep_once()

    recovered = (await _load(session_factory))["checkpoint-failure"]
    assert recovered.swept is True
    assert recovered.collected is False
    assert recovered.sweep_started_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("redemption_succeeds", [True, False])
async def test_expired_worker_cannot_overwrite_or_release_newer_claim(
    session_factory: async_sessionmaker[AsyncSession],
    redemption_succeeds: bool,
) -> None:
    await _insert(
        session_factory,
        CashuTransaction(
            token="reclaimed",
            amount=1,
            unit="sat",
            type="out",
            created_at=800,
        ),
    )

    async def replace_claim(_token: str) -> tuple[int, str, str]:
        async with session_factory() as session:
            result = await session.exec(
                select(CashuTransaction).where(CashuTransaction.token == "reclaimed")
            )
            transaction = result.one()
            transaction.sweep_started_at = 1100
            session.add(transaction)
            await session.commit()
        if not redemption_succeeds:
            raise RuntimeError("mint unavailable")
        return (1, "sat", "mint")

    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.time.time", return_value=1000),
        patch("routstr.wallet.recieve_token", AsyncMock(side_effect=replace_claim)),
    ):
        await refund_sweep_once()

    reclaimed = (await _load(session_factory))["reclaimed"]
    assert reclaimed.swept is False
    assert reclaimed.collected is False
    assert reclaimed.sweep_started_at == 1100


@pytest.mark.asyncio
async def test_refund_sweep_recovers_stale_claim_without_misreporting_collection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert(
        session_factory,
        CashuTransaction(
            token="stale",
            amount=1,
            unit="sat",
            type="out",
            created_at=800,
            sweep_started_at=100,
        ),
        CashuTransaction(
            token="active",
            amount=1,
            unit="sat",
            type="out",
            created_at=800,
            sweep_started_at=950,
        ),
    )
    receive = AsyncMock(side_effect=RuntimeError("token already spent"))
    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.settings.refund_sweep_claim_timeout_seconds", 200),
        patch("routstr.wallet.time.time", return_value=1000),
        patch("routstr.wallet.recieve_token", receive),
    ):
        await refund_sweep_once()

    receive.assert_awaited_once_with("stale")
    loaded = await _load(session_factory)
    assert loaded["stale"].swept is True
    assert loaded["stale"].collected is False
    assert loaded["stale"].sweep_started_at is None
    assert loaded["active"].swept is False
    assert loaded["active"].sweep_started_at == 950
