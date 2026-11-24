from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import AnalyticsV2Outbox, TerminalOutcome, TerminalOutcomeEpoch
from routstr.nostr.analytics_v2 import AnalyticsV2Error
from routstr.nostr.analytics_v2_delivery import (
    AnalyticsV2DeliveryError,
    AnalyticsV2Producer,
    SharingDisabledError,
    activate_analytics_v2_sharing,
    claim_analytics_v2_identity,
    list_unpublished_analytics_v2_epochs,
    release_analytics_v2_epochs,
    transition_analytics_v2_sharing,
)

PRIVATE_KEY = "11" * 32
PUBLIC_KEY = Keys.parse(PRIVATE_KEY).public_key().to_hex()
WEEK = date(2026, 8, 31)
OPT_IN_DAY = WEEK + timedelta(days=2)
NOW = datetime(2026, 9, 8, 0, 1, tzinfo=UTC)


def _at_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def _outcome(outcome_id: str, terminal_day: date) -> TerminalOutcome:
    return TerminalOutcome(
        outcome_id=outcome_id,
        terminal_at_ms=1,
        terminal_day=terminal_day,
        model_identifier="model/served",
        input_tokens=1,
        output_tokens=1,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        revenue_msats=1,
        input_source="reported",
        output_source="reported",
        cache_read_source="missing",
        cache_creation_source="missing",
    )


@pytest_asyncio.fixture
async def session_factory(
    analytics_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(
        analytics_engine, class_=AsyncSession, expire_on_commit=False
    )
    # Two weeks of private collection before the operator opted in mid-week.
    async with factory() as session:
        session.add(
            TerminalOutcomeEpoch(
                epoch=0, coverage_start_day=WEEK - timedelta(days=14), current_slot=1
            )
        )
        session.add(_outcome("pre-opt-in", WEEK - timedelta(days=10)))
        session.add(_outcome("shared-week-before", WEEK))
        session.add(_outcome("shared-week-after", WEEK + timedelta(days=4)))
        await session.commit()
    await claim_analytics_v2_identity(
        factory, pubkey=PUBLIC_KEY, provider_d="provider", at_ms=1
    )
    await activate_analytics_v2_sharing(
        factory, coverage_day=OPT_IN_DAY, at_ms=_at_ms(OPT_IN_DAY)
    )
    yield factory


def _producer(factory: async_sessionmaker[AsyncSession]) -> AnalyticsV2Producer:
    return AnalyticsV2Producer(
        factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )


async def _outbox(factory: async_sessionmaker[AsyncSession]) -> list[AnalyticsV2Outbox]:
    async with factory() as session:
        return list(
            (
                await session.exec(
                    select(AnalyticsV2Outbox).order_by(
                        col(AnalyticsV2Outbox.epoch), col(AnalyticsV2Outbox.week)
                    )
                )
            ).all()
        )


def _days(row: AnalyticsV2Outbox) -> set[str]:
    return set(json.loads(json.loads(row.frame)[1]["content"])["days"])


@pytest.mark.asyncio
async def test_released_pre_opt_in_epoch_publishes_disjoint_days(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    producer = _producer(session_factory)
    assert await producer.produce_once(now=NOW) == 1
    assert [row.epoch for row in await _outbox(session_factory)] == [1]

    state, candidates = await list_unpublished_analytics_v2_epochs(
        session_factory, today_utc=NOW.date()
    )
    assert [(c.epoch, c.coverage_end, c.requests, c.released) for c in candidates] == [
        (0, OPT_IN_DAY - timedelta(days=1), 2, False)
    ]

    assert await release_analytics_v2_epochs(
        session_factory, [0], expected_generation=state.generation
    ) == (0,)
    assert await producer.produce_once(now=NOW) == 2
    assert await producer.produce_once(now=NOW) == 0

    rows = await _outbox(session_factory)
    assert [(row.epoch, row.week) for row in rows] == [
        (0, WEEK - timedelta(days=14)),
        (0, WEEK),
        (1, WEEK),
    ]
    shared = [row for row in rows if row.week == WEEK]
    assert _days(shared[0]).isdisjoint(_days(shared[1]))
    assert WEEK.isoformat() in _days(shared[0])
    assert (WEEK + timedelta(days=4)).isoformat() in _days(shared[1])

    _, candidates = await list_unpublished_analytics_v2_epochs(
        session_factory, today_utc=NOW.date()
    )
    assert [(c.epoch, c.released) for c in candidates] == [(0, True)]


@pytest.mark.asyncio
async def test_release_refuses_floor_epoch_and_stale_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    state, _ = await list_unpublished_analytics_v2_epochs(session_factory)
    with pytest.raises(AnalyticsV2DeliveryError, match="cannot be released"):
        await release_analytics_v2_epochs(
            session_factory, [1], expected_generation=state.generation
        )
    with pytest.raises(AnalyticsV2DeliveryError, match="cannot be released"):
        await release_analytics_v2_epochs(
            session_factory, [7], expected_generation=state.generation
        )
    with pytest.raises(SharingDisabledError):
        await release_analytics_v2_epochs(
            session_factory, [0], expected_generation=state.generation + 1
        )


@pytest.mark.asyncio
async def test_sharing_transition_withdraws_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    state, _ = await list_unpublished_analytics_v2_epochs(session_factory)
    await release_analytics_v2_epochs(
        session_factory, [0], expected_generation=state.generation
    )
    off_day = OPT_IN_DAY + timedelta(days=1)
    await transition_analytics_v2_sharing(
        session_factory, enabled=False, at_ms=_at_ms(off_day)
    )
    on_day = off_day + timedelta(days=1)
    await activate_analytics_v2_sharing(
        session_factory, coverage_day=on_day, at_ms=_at_ms(on_day)
    )

    await _producer(session_factory).produce_once(now=NOW)
    assert 0 not in {row.epoch for row in await _outbox(session_factory)}
    _, candidates = await list_unpublished_analytics_v2_epochs(
        session_factory, today_utc=NOW.date()
    )
    assert [(c.epoch, c.released) for c in candidates if c.epoch == 0] == [(0, False)]


@pytest.mark.asyncio
async def test_epoch_with_outbox_rows_is_not_releasable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    producer = _producer(session_factory)
    await producer.produce_once(now=NOW)
    off_day = OPT_IN_DAY + timedelta(days=5)
    await transition_analytics_v2_sharing(
        session_factory, enabled=False, at_ms=_at_ms(off_day)
    )
    await activate_analytics_v2_sharing(
        session_factory, coverage_day=off_day, at_ms=_at_ms(off_day)
    )
    state, candidates = await list_unpublished_analytics_v2_epochs(
        session_factory, today_utc=NOW.date()
    )
    assert 1 not in {c.epoch for c in candidates}
    with pytest.raises(AnalyticsV2DeliveryError, match="cannot be released"):
        await release_analytics_v2_epochs(
            session_factory, [1], expected_generation=state.generation
        )


@pytest.mark.asyncio
async def test_failing_released_epoch_does_not_block_current_epoch(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = await list_unpublished_analytics_v2_epochs(session_factory)
    await release_analytics_v2_epochs(
        session_factory, [0], expected_generation=state.generation
    )
    producer = _producer(session_factory)
    original = producer._next_aggregate

    def reject_released(*args: Any, **kwargs: Any) -> Any:
        if kwargs["epoch"].epoch == 0:
            raise AnalyticsV2Error("invalid backlog week")
        return original(*args, **kwargs)

    monkeypatch.setattr(producer, "_next_aggregate", reject_released)
    assert await producer.produce_once(now=NOW) == 1
    assert [row.epoch for row in await _outbox(session_factory)] == [1]


@pytest.mark.asyncio
async def test_release_endpoint_maps_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from routstr.core import admin

    async def stale(*args: Any, **kwargs: Any) -> tuple[int, ...]:
        raise SharingDisabledError("changed")

    async def invalid(*args: Any, **kwargs: Any) -> tuple[int, ...]:
        raise AnalyticsV2DeliveryError("Epoch 3 cannot be released")

    body = admin.AnalyticsHistoryRelease(epochs=[3], generation=1)
    for fake, status in ((stale, 409), (invalid, 400)):
        monkeypatch.setattr(admin, "release_analytics_v2_epochs", fake)
        with pytest.raises(HTTPException) as raised:
            await admin.release_analytics_sharing_history(body)
        assert raised.value.status_code == status
