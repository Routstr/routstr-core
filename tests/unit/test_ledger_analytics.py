from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import (
    TerminalOutcome,
    TerminalOutcomeEpoch,
    TerminalOutcomeWriterRun,
)
from routstr.core.ledger_analytics import get_ledger_usage_dashboard
from routstr.core.terminal_outcome_writer import SessionFactory

NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[SessionFactory, None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dashboard.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def factory() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    yield factory
    await engine.dispose()


def _row(
    name: str,
    at: datetime,
    *,
    model: str | None = "reported/model",
    revenue: int = 1000,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read: int = 2,
    cache_write: int = 1,
    source: str = "reported",
) -> TerminalOutcome:
    return TerminalOutcome(
        outcome_id=name,
        terminal_at_ms=int(at.timestamp() * 1000),
        terminal_day=at.date(),
        model_identifier=model,
        revenue_msats=revenue,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        input_source=source,
        output_source=source,
        cache_read_source="reported" if cache_read else "missing",
        cache_creation_source="reported" if cache_write else "missing",
    )


def _legacy() -> dict:
    return {
        "summary": {
            "total_requests": 9,
            "successful_chat_completions": 999,
            "failed_requests": 2,
            "total_errors": 3,
            "refunds_msats": 9000,
            "success_rate": 42,
            "revenue_msats": 9999,
            "net_revenue_msats": 999,
            "input_tokens": 999,
            "output_tokens": 999,
            "total_tokens": 1998,
            "avg_latency_ms": 45,
        },
        "metrics": {
            "metrics": [
                {
                    "timestamp": "2026-09-17 12:00:00",
                    "total_requests": 9,
                    "failed_requests": 2,
                    "errors": 3,
                    "refunds_msats": 9000,
                    "successful_chat_completions": 999,
                    "revenue_msats": 9999,
                    "input_tokens": 999,
                    "output_tokens": 999,
                    "total_tokens": 1998,
                }
            ],
            "totals": {
                "total_requests": 9,
                "failed_requests": 2,
                "refunds_msats": 9000,
                "successful_chat_completions": 999,
                "revenue_msats": 9999,
            },
        },
        "error_details": {
            "errors": [{"message": "upstream timeout"}],
            "total_count": 3,
        },
        "revenue_by_model": {
            "models": [
                {
                    "model": "reported/model",
                    "requests": 7,
                    "failed": 2,
                    "refunds_sats": 9,
                }
            ]
        },
    }


async def _seed(sessions: SessionFactory) -> None:
    async with sessions() as session:
        session.add_all(
            [
                _row("before", NOW - timedelta(days=1, milliseconds=1), revenue=9999),
                _row("start", NOW - timedelta(days=1)),
                _row(
                    "free",
                    NOW - timedelta(hours=4),
                    model="free/model",
                    revenue=0,
                    input_tokens=12,
                    output_tokens=3,
                    cache_read=0,
                    cache_write=0,
                    source="estimated",
                ),
                _row(
                    "unknown",
                    NOW - timedelta(hours=2),
                    model=None,
                    revenue=250,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read=0,
                    cache_write=0,
                    source="missing",
                ),
                _row("at-end", NOW, revenue=9999),
                _row("future", NOW + timedelta(hours=1), revenue=9999),
                TerminalOutcomeEpoch(
                    epoch=0, coverage_start_day=date(2026, 9, 17), current_slot=1
                ),
            ]
        )
        session.add(
            TerminalOutcomeWriterRun(
                run_id="active-run",
                status="active",
                started_at_ms=int((NOW - timedelta(days=1)).timestamp() * 1000),
                heartbeat_at_ms=int(NOW.timestamp() * 1000),
                flushed_through_ms=int(NOW.timestamp() * 1000),
            )
        )
        await session.commit()


async def test_dashboard_replaces_log_totals_and_matches_every_chart(
    sessions: SessionFactory,
) -> None:
    await _seed(sessions)
    legacy = _legacy()
    before = deepcopy(legacy)
    result = await get_ledger_usage_dashboard(
        legacy,
        interval=60,
        hours=24,
        model_limit=1,
        session_factory=sessions,
        now=NOW,
    )
    summary = result["summary"]
    assert summary["successful_chat_completions"] == 3
    assert summary["revenue_msats"] == summary["net_revenue_msats"] == 1250
    assert summary["input_tokens"] == 25
    assert summary["output_tokens"] == 8
    assert summary["total_tokens"] == 33
    assert summary["total_requests"] == 9 and summary["failed_requests"] == 2
    assert summary["refunds_msats"] == 9000
    assert summary["success_rate"] == 42 and summary["avg_latency_ms"] == 45
    assert result["error_details"] == before["error_details"]
    assert legacy == before
    assert result["analytics_source"] == "terminal_outcomes"
    for field in ("successful_chat_completions", "revenue_msats", "total_tokens"):
        assert (
            sum(point[field] for point in result["metrics"]["metrics"])
            == summary[field]
        )
        assert result["metrics"]["totals"][field] == summary[field]
    mix = result["model_usage_mix"]
    assert sum(point["total_successful"] for point in mix["metrics"]) == 3
    assert sum(point["total_revenue_msats"] for point in mix["metrics"]) == 1250
    assert sum(point["total_tokens"] for point in mix["metrics"]) == 33
    assert sum(point["others"] for point in mix["metrics"]) == 1
    assert "free/model" in mix["top_models_by_metric"]["requests"]
    assert summary["unique_models_count"] == 2
    assert result["revenue_by_model"]["models"][0]["successful"] == 1
    assert result["revenue_by_model"]["total_revenue_sats"] == 1.25
    coverage = result["ledger_coverage"]
    assert coverage["complete"] and coverage["diagnostic_available"]
    assert coverage["includes_current_day"]
    assert coverage["token_sources"]["input"] == {
        "reported": 1,
        "estimated": 1,
        "missing": 1,
    }
    assert coverage["token_sources"]["cache_read"] == {
        "reported": 1,
        "estimated": 0,
        "missing": 2,
    }


async def test_historical_dates_do_not_return_recent_ledger_or_log_data(
    sessions: SessionFactory,
) -> None:
    await _seed(sessions)
    start, end = datetime(2026, 9, 17, tzinfo=UTC), datetime(2026, 9, 18, tzinfo=UTC)
    result = await get_ledger_usage_dashboard(
        _legacy(),
        interval=60,
        hours=24,
        session_factory=sessions,
        now=NOW,
        start_at=start,
        end_at=end,
    )
    assert result["summary"]["successful_chat_completions"] == 2
    assert result["summary"]["revenue_msats"] == 10_999
    assert result["summary"]["total_requests"] == 0
    assert result["summary"]["refunds_msats"] == 0
    assert result["summary"]["success_rate"] == 0
    assert result["error_details"] == {"errors": [], "total_count": 0}
    assert all(
        point["timestamp"].startswith("2026-09-17")
        for point in result["metrics"]["metrics"]
    )
    coverage = result["ledger_coverage"]
    assert coverage["from"] == start.isoformat() and coverage["to"] == end.isoformat()
    assert not coverage["diagnostic_available"] and not coverage["includes_current_day"]


async def test_empty_ledger_has_no_fallback_to_legacy_successes(
    sessions: SessionFactory,
) -> None:
    result = await get_ledger_usage_dashboard(
        _legacy(),
        interval=60,
        hours=24,
        session_factory=sessions,
        now=NOW,
    )
    assert result["summary"]["successful_chat_completions"] == 0
    assert result["summary"]["total_tokens"] == 0
    assert result["summary"]["revenue_msats"] == 0
    assert result["summary"]["total_requests"] == 9
    assert len(result["model_usage_mix"]["metrics"]) == 24
    assert all(
        point["coverage"] == "missing" and point["total_successful"] is None
        for point in result["model_usage_mix"]["metrics"]
    )
    assert result["ledger_coverage"]["incomplete_days"] == ["2026-09-17", "2026-09-18"]
    assert not result["ledger_coverage"]["complete"]
    assert result["ledger_coverage"]["latest_outcome_at"] is None


async def test_closed_epoch_preserves_history_and_exposes_collection_gap(
    sessions: SessionFactory,
) -> None:
    async with sessions() as session:
        session.add_all(
            [
                TerminalOutcomeEpoch(
                    epoch=0,
                    coverage_start_day=date(2026, 9, 15),
                    coverage_end_day=date(2026, 9, 16),
                    current_slot=None,
                ),
                TerminalOutcomeEpoch(
                    epoch=1, coverage_start_day=date(2026, 9, 19), current_slot=1
                ),
                _row("historical", datetime(2026, 9, 16, 12, tzinfo=UTC)),
            ]
        )
        await session.commit()
    result = await get_ledger_usage_dashboard(
        _legacy(),
        interval=60,
        hours=24,
        session_factory=sessions,
        now=NOW,
        start_at=datetime(2026, 9, 15, tzinfo=UTC),
        end_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    assert result["summary"]["successful_chat_completions"] == 1
    assert result["ledger_coverage"]["incomplete_days"] == ["2026-09-17", "2026-09-18"]
    assert result["metrics"]["hours_back"] == 96


async def test_model_limit_keeps_omitted_models_in_other_totals(
    sessions: SessionFactory,
) -> None:
    async with sessions() as session:
        session.add_all(
            [
                _row(
                    f"model-{i}",
                    NOW - timedelta(hours=1),
                    model=f"model/{i}",
                    revenue=i,
                )
                for i in range(25)
            ]
        )
        await session.commit()
    result = await get_ledger_usage_dashboard(
        _legacy(),
        interval=60,
        hours=24,
        model_limit=2,
        session_factory=sessions,
        now=NOW,
    )
    mix = next(
        point
        for point in result["model_usage_mix"]["metrics"]
        if point["total_successful"]
    )
    assert result["summary"]["unique_models_count"] == 25
    assert len(result["revenue_by_model"]["models"]) == 2
    assert sum(mix["model_counts"].values()) + mix["others"] == 25
    assert sum(mix["model_revenue_msats"].values()) + mix[
        "others_revenue_msats"
    ] == sum(range(25))
    assert sum(mix["model_tokens"].values()) + mix["others_tokens"] == 25 * 18


async def test_future_part_of_custom_period_is_incomplete_and_excluded(
    sessions: SessionFactory,
) -> None:
    await _seed(sessions)
    result = await get_ledger_usage_dashboard(
        _legacy(),
        interval=60,
        hours=48,
        session_factory=sessions,
        now=NOW,
        start_at=datetime(2026, 9, 18, tzinfo=UTC),
        end_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    assert result["summary"]["successful_chat_completions"] == 2
    assert result["summary"]["revenue_msats"] == 250
    assert result["ledger_coverage"]["incomplete_days"] == ["2026-09-18", "2026-09-19"]
    assert not result["ledger_coverage"]["complete"]


@pytest.mark.parametrize(
    ("lag", "incomplete_days", "idle_hour", "open_hour"),
    [
        (timedelta(seconds=10), [], ("complete", 0), "updating"),
        (timedelta(days=1), ["2026-09-17", "2026-09-18"], ("missing", None), "missing"),
    ],
)
async def test_trailing_checkpoint_is_a_gap_only_when_left_in_an_earlier_day(
    sessions: SessionFactory,
    lag: timedelta,
    incomplete_days: list[str],
    idle_hour: tuple[str, int | None],
    open_hour: str,
) -> None:
    await _seed(sessions)
    async with sessions() as session:
        run = await session.get(TerminalOutcomeWriterRun, "active-run")
        assert run is not None
        run.flushed_through_ms = int((NOW - lag).timestamp() * 1000)
        session.add(run)
        await session.commit()
    result = await get_ledger_usage_dashboard(
        _legacy(),
        interval=60,
        hours=24,
        session_factory=sessions,
        now=NOW,
    )
    assert result["summary"]["successful_chat_completions"] == 3
    assert result["ledger_coverage"]["incomplete_days"] == incomplete_days
    points = {point["timestamp"]: point for point in result["metrics"]["metrics"]}
    idle = points["2026-09-18 09:00:00"]
    assert (idle["coverage"], idle["revenue_msats"]) == idle_hour
    assert points["2026-09-18 11:00:00"]["coverage"] == open_hour


async def test_degraded_writer_exposes_gap_before_epoch_rotation(
    sessions: SessionFactory,
) -> None:
    await _seed(sessions)
    async with sessions() as session:
        run = await session.get(TerminalOutcomeWriterRun, "active-run")
        assert run is not None
        run.status = "degraded"
        run.loss_day = date(2026, 9, 17)
        session.add(run)
        await session.commit()
    result = await get_ledger_usage_dashboard(
        _legacy(),
        interval=60,
        hours=24,
        session_factory=sessions,
        now=NOW,
    )
    assert result["ledger_coverage"]["incomplete_days"] == ["2026-09-17", "2026-09-18"]
    assert not result["ledger_coverage"]["complete"]


async def test_live_writer_loss_is_visible_before_it_can_persist_gap(
    sessions: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from routstr.core import terminal_outcomes

    await _seed(sessions)
    monkeypatch.setattr(
        terminal_outcomes,
        "terminal_outcome_writer",
        SimpleNamespace(
            loss_pending=True,
            loss_day=date(2026, 9, 17),
        ),
    )
    result = await get_ledger_usage_dashboard(
        _legacy(),
        interval=60,
        hours=24,
        session_factory=sessions,
        now=NOW,
    )
    assert result["ledger_coverage"]["incomplete_days"] == ["2026-09-17", "2026-09-18"]
    assert not result["ledger_coverage"]["complete"]


async def test_chart_fills_covered_zero_buckets_without_fabricating_missing_days(
    sessions: SessionFactory,
) -> None:
    async with sessions() as session:
        session.add_all(
            [
                TerminalOutcomeEpoch(
                    epoch=0,
                    coverage_start_day=date(2026, 9, 14),
                    coverage_end_day=date(2026, 9, 16),
                    current_slot=None,
                ),
                _row("complete", datetime(2026, 9, 14, 12, tzinfo=UTC)),
                _row("partial", datetime(2026, 9, 17, 12, tzinfo=UTC), revenue=250),
            ]
        )
        await session.commit()
    result = await get_ledger_usage_dashboard(
        _legacy(),
        interval=1440,
        hours=120,
        session_factory=sessions,
        now=NOW,
        start_at=datetime(2026, 9, 13, tzinfo=UTC),
        end_at=datetime(2026, 9, 18, tzinfo=UTC),
    )
    points = result["metrics"]["metrics"]
    mix = result["model_usage_mix"]["metrics"]
    assert len(points) == len(mix) == 5
    assert [point["coverage"] for point in points] == [
        "missing",
        "complete",
        "complete",
        "complete",
        "partial",
    ]
    assert [point["revenue_msats"] for point in points] == [None, 1000, 0, 0, 250]
    assert [point["timestamp"] for point in points] == [
        point["timestamp"] for point in mix
    ]
    assert [point["total_revenue_msats"] for point in mix] == [None, 1000, 0, 0, 250]
    assert mix[0]["model_counts"] == mix[2]["model_counts"] == {}
    assert result["summary"]["revenue_msats"] == 1250
    assert result["metrics"]["totals"]["successful_chat_completions"] == 2
    assert result["metrics"]["bucket_fill_complete"]
    assert result["model_usage_mix"]["bucket_fill_complete"]


async def test_chart_keeps_requested_interval_and_bounds_bucket_filling(
    sessions: SessionFactory,
) -> None:
    await _seed(sessions)
    result = await get_ledger_usage_dashboard(
        _legacy(), interval=1, hours=24, session_factory=sessions, now=NOW
    )
    assert result["metrics"]["interval_minutes"] == 1
    assert result["model_usage_mix"]["interval_minutes"] == 1
    assert not result["metrics"]["bucket_fill_complete"]
    assert not result["model_usage_mix"]["bucket_fill_complete"]
    assert len(result["metrics"]["metrics"]) == 3
    assert result["summary"]["revenue_msats"] == 1250


async def test_measured_average_uses_paired_reported_requests_including_free_and_zero(
    sessions: SessionFactory,
) -> None:
    at = NOW - timedelta(hours=1)
    input_only = _row(
        "input-only",
        at,
        input_tokens=9,
        output_tokens=0,
        cache_read=0,
        cache_write=0,
        source="missing",
    )
    input_only.input_source = "reported"
    output_only = _row(
        "output-only",
        at,
        input_tokens=0,
        output_tokens=7,
        cache_read=0,
        cache_write=0,
        source="missing",
    )
    output_only.output_source = "reported"
    async with sessions() as session:
        session.add_all(
            [
                _row("reported-cache", at),
                _row(
                    "free",
                    at,
                    model=None,
                    revenue=0,
                    input_tokens=6,
                    output_tokens=4,
                    cache_read=0,
                    cache_write=0,
                ),
                _row(
                    "measured-zero",
                    at,
                    revenue=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read=0,
                    cache_write=0,
                ),
                input_only,
                output_only,
                _row(
                    "estimated",
                    at,
                    input_tokens=20,
                    output_tokens=10,
                    cache_read=0,
                    cache_write=0,
                    source="estimated",
                ),
            ]
        )
        await session.commit()
    result = await get_ledger_usage_dashboard(
        _legacy(), interval=60, hours=24, session_factory=sessions, now=NOW
    )
    summary = result["summary"]
    assert summary["measured_token_requests"] == 3
    assert summary["measured_tokens"] == 28
    assert summary["avg_measured_tokens_per_completion"] == pytest.approx(28 / 3)
    assert summary["successful_chat_completions"] == 6
    assert summary["total_tokens"] == 74
    assert summary["avg_total_tokens_per_completion"] == pytest.approx(74 / 6)
    assert summary["total_requests"] == 9
    assert summary["success_rate"] == 42
    assert "measured_tokens" not in result["metrics"]["totals"]


@pytest.mark.parametrize("dimension", ["cache_read", "cache_creation"])
@pytest.mark.parametrize(
    ("source", "count", "eligible"),
    [
        ("reported", 3, True),
        ("missing", 0, True),
        ("missing", 3, False),
        ("estimated", 0, False),
        ("estimated", 3, False),
    ],
)
async def test_measured_average_requires_reliable_nonzero_cache_counts(
    sessions: SessionFactory,
    dimension: str,
    source: str,
    count: int,
    eligible: bool,
) -> None:
    row = _row("cache", NOW - timedelta(hours=1), cache_read=0, cache_write=0)
    setattr(row, dimension + "_source", source)
    setattr(row, dimension + "_input_tokens", count)
    async with sessions() as session:
        session.add(row)
        await session.commit()
    result = await get_ledger_usage_dashboard(
        _legacy(), interval=60, hours=24, session_factory=sessions, now=NOW
    )
    summary = result["summary"]
    assert summary["measured_token_requests"] == int(eligible)
    assert summary["measured_tokens"] == (15 + count if eligible else 0)
    assert summary["avg_measured_tokens_per_completion"] == (
        15 + count if eligible else None
    )
    assert summary["total_tokens"] == 15 + count


@pytest.mark.parametrize("scenario", ["empty", "unpaired", "measured-zero"])
async def test_measured_average_distinguishes_unknown_from_measured_zero(
    sessions: SessionFactory,
    scenario: str,
) -> None:
    rows = []
    if scenario == "unpaired":
        input_only = _row(
            "input", NOW - timedelta(hours=1), cache_read=0, cache_write=0
        )
        input_only.output_source = "missing"
        output_only = _row(
            "output", NOW - timedelta(hours=1), cache_read=0, cache_write=0
        )
        output_only.input_source = "missing"
        rows = [input_only, output_only]
    elif scenario == "measured-zero":
        rows = [
            _row(
                "zero",
                NOW - timedelta(hours=1),
                input_tokens=0,
                output_tokens=0,
                cache_read=0,
                cache_write=0,
                revenue=0,
            )
        ]
    async with sessions() as session:
        session.add_all(rows)
        await session.commit()
    result = await get_ledger_usage_dashboard(
        _legacy(), interval=60, hours=24, session_factory=sessions, now=NOW
    )
    summary = result["summary"]
    assert summary["measured_token_requests"] == (
        1 if scenario == "measured-zero" else 0
    )
    assert summary["measured_tokens"] == 0
    assert summary["avg_measured_tokens_per_completion"] == (
        0 if scenario == "measured-zero" else None
    )


@pytest.mark.parametrize("source", ["reported", "estimated", "missing"])
async def test_measured_average_admits_only_reported_sources(
    sessions: SessionFactory,
    source: str,
) -> None:
    row = _row(
        "provenance",
        NOW - timedelta(hours=1),
        cache_read=0,
        cache_write=0,
        source=source,
    )
    async with sessions() as session:
        session.add(row)
        await session.commit()
    result = await get_ledger_usage_dashboard(
        _legacy(), interval=60, hours=24, session_factory=sessions, now=NOW
    )
    eligible = source == "reported"
    assert result["summary"]["measured_token_requests"] == int(eligible)
    assert result["summary"]["measured_tokens"] == (15 if eligible else 0)
    assert result["ledger_coverage"]["token_sources"]["input"] == {
        "reported": int(eligible),
        "estimated": int(source == "estimated"),
        "missing": int(not eligible and source != "estimated"),
    }


async def test_measured_average_uses_exact_historical_bounds(
    sessions: SessionFactory,
) -> None:
    start, end = datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC)
    async with sessions() as session:
        session.add_all(
            [
                _row("before", start - timedelta(milliseconds=1)),
                _row(
                    "start",
                    start,
                    input_tokens=8,
                    output_tokens=4,
                    cache_read=0,
                    cache_write=0,
                ),
                _row(
                    "last",
                    end - timedelta(milliseconds=1),
                    input_tokens=4,
                    output_tokens=2,
                    cache_read=0,
                    cache_write=0,
                ),
                _row("end", end),
                _row("recent", NOW - timedelta(hours=1)),
            ]
        )
        await session.commit()
    result = await get_ledger_usage_dashboard(
        _legacy(),
        interval=60,
        hours=24,
        session_factory=sessions,
        now=NOW,
        start_at=start,
        end_at=end,
    )
    summary = result["summary"]
    assert summary["measured_token_requests"] == 2
    assert summary["measured_tokens"] == 18
    assert summary["avg_measured_tokens_per_completion"] == 9
