from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, text
from sqlmodel import col, func, select

from . import terminal_outcomes
from .db import (
    TerminalOutcome,
    TerminalOutcomeEpoch,
    TerminalOutcomeWriterRun,
    create_session,
)
from .terminal_outcome_writer import SessionFactory

_SETTLED_FIELDS = (
    "successful_chat_completions",
    "revenue_msats",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "total_tokens",
)
_DIAGNOSTIC_FIELDS = (
    "total_requests",
    "failed_requests",
    "errors",
    "warnings",
    "payment_processed",
    "upstream_errors",
    "refunds_msats",
    "requests",
)
_DIMENSIONS = ("input", "output", "cache_read", "cache_creation")


def _measures() -> list[Any]:
    input_tokens = (
        col(TerminalOutcome.input_tokens)
        + col(TerminalOutcome.cache_read_input_tokens)
        + col(TerminalOutcome.cache_creation_input_tokens)
    )
    return [
        func.count(col(TerminalOutcome.outcome_id)).label(
            "successful_chat_completions"
        ),
        func.sum(TerminalOutcome.revenue_msats).label("revenue_msats"),
        func.sum(input_tokens).label("input_tokens"),
        func.sum(TerminalOutcome.output_tokens).label("output_tokens"),
        func.sum(TerminalOutcome.cache_read_input_tokens).label(
            "cache_read_input_tokens"
        ),
        func.sum(TerminalOutcome.cache_creation_input_tokens).label(
            "cache_creation_input_tokens"
        ),
        func.sum(input_tokens + col(TerminalOutcome.output_tokens)).label(
            "total_tokens"
        ),
    ]


def _values(row: Any) -> dict[str, int]:
    return {name: int(getattr(row, name) or 0) for name in _SETTLED_FIELDS}


def _timestamp(bucket: int, bucket_ms: int) -> str:
    return datetime.fromtimestamp(bucket * bucket_ms / 1000, UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _coverage_days(
    start: datetime, end: datetime, epochs: list[TerminalOutcomeEpoch], clock: datetime
) -> list[str]:
    missing = []
    day = start.date()
    last = (end - timedelta(milliseconds=1)).date()
    while day <= last:
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
        if min(day_end, end) > clock or not any(
            epoch.coverage_start_day <= day
            and (epoch.coverage_end_day is None or day <= epoch.coverage_end_day)
            for epoch in epochs
        ):
            missing.append(day.isoformat())
        day += timedelta(days=1)
    return missing


async def get_ledger_usage_dashboard(
    legacy_dashboard: dict[str, Any],
    *,
    interval: int,
    hours: int,
    model_limit: int = 20,
    session_factory: SessionFactory = create_session,
    now: datetime | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict[str, Any]:
    """Overlay settled records while retaining log-only request diagnostics."""
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    clock = clock.astimezone(UTC)
    end = end_at or clock
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    end = end.astimezone(UTC)
    start = start_at or end - timedelta(hours=hours)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    start = start.astimezone(UTC)
    if end <= start:
        raise ValueError("Stats period end must be after its start")
    diagnostic_available = start_at is None and end_at is None
    window_hours = (end - start).total_seconds() / 3600
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    bucket_ms = max(1, interval) * 60_000
    limit = max(1, min(model_limit, 100))
    top_limit = min(limit, 20)
    bucket = (col(TerminalOutcome.terminal_at_ms) // bucket_ms).label("bucket")
    model = func.nullif(TerminalOutcome.model_identifier, "").label("model")
    bounds = (
        col(TerminalOutcome.terminal_day) >= start.date(),
        col(TerminalOutcome.terminal_day) <= end.date(),
        col(TerminalOutcome.terminal_at_ms) >= start_ms,
        col(TerminalOutcome.terminal_at_ms)
        < min(end_ms, int(clock.timestamp() * 1000)),
    )
    measures = _measures()
    total_tokens = measures[-1]
    provenance = []
    sources = {}
    for dimension in _DIMENSIONS:
        source = col(getattr(TerminalOutcome, dimension + "_source"))
        sources[dimension] = source
        for status in ("reported", "estimated", "missing"):
            provenance.append(
                func.sum(case((source == status, 1), else_=0)).label(
                    f"{dimension}_{status}"
                )
            )
    measured = (sources["input"] == "reported") & (sources["output"] == "reported")
    for dimension in ("cache_read", "cache_creation"):
        cache_tokens = col(getattr(TerminalOutcome, dimension + "_input_tokens"))
        measured &= (sources[dimension] == "reported") | (
            (sources[dimension] == "missing") & (cache_tokens == 0)
        )
    recorded_tokens = (
        col(TerminalOutcome.input_tokens)
        + col(TerminalOutcome.output_tokens)
        + col(TerminalOutcome.cache_read_input_tokens)
        + col(TerminalOutcome.cache_creation_input_tokens)
    )
    async with session_factory() as session:
        # Keep all aggregates on one snapshot while queued outcomes are flushed.
        if session.get_bind().dialect.name == "sqlite":
            await session.execute(text("BEGIN"))
        else:
            await session.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
        bucket_rows = (
            await session.exec(
                select(bucket, *measures)
                .where(*bounds)
                .group_by(bucket)
                .order_by(bucket)
            )
        ).all()
        top_models: dict[str, list[str]] = {}
        for metric, measure in (
            ("requests", measures[0]),
            ("revenue", measures[1]),
            ("tokens", total_tokens),
        ):
            rows = (
                await session.exec(
                    select(model, measure)
                    .where(*bounds, model.is_not(None))
                    .group_by(model)
                    .having(measure > 0)
                    .order_by(measure.desc(), model)
                    .limit(top_limit)
                )
            ).all()
            top_models[metric] = [str(row[0]) for row in rows]
        selected = sorted(set(name for names in top_models.values() for name in names))
        model_bucket_rows = (
            (
                await session.exec(
                    select(bucket, model, *measures)
                    .where(*bounds, model.in_(selected))
                    .group_by(bucket, model)
                    .order_by(bucket, model)
                )
            ).all()
            if selected
            else []
        )
        model_rows = (
            await session.exec(
                select(model, *measures)
                .where(*bounds)
                .group_by(model)
                .order_by(measures[1].desc(), model)
                .limit(limit)
            )
        ).all()
        model_count = (
            await session.exec(select(func.count(func.distinct(model))).where(*bounds))
        ).one()
        observation = (
            (
                await session.execute(
                    select(
                        func.max(TerminalOutcome.terminal_at_ms).label("latest"),
                        func.max(case((model.is_(None), 1), else_=0)).label(
                            "unattributed"
                        ),
                        func.sum(case((measured, 1), else_=0)).label(
                            "measured_token_requests"
                        ),
                        func.sum(case((measured, recorded_tokens), else_=0)).label(
                            "measured_tokens"
                        ),
                        *provenance,
                    ).where(*bounds)
                )
            )
            .mappings()
            .one()
        )
        epochs = list(
            (
                await session.exec(
                    select(TerminalOutcomeEpoch)
                    .where(col(TerminalOutcomeEpoch.coverage_start_day) <= end.date())
                    .where(
                        col(TerminalOutcomeEpoch.coverage_end_day).is_(None)
                        | (col(TerminalOutcomeEpoch.coverage_end_day) >= start.date())
                    )
                )
            ).all()
        )

        runs = list(
            (
                await session.exec(
                    select(TerminalOutcomeWriterRun).where(
                        col(TerminalOutcomeWriterRun.status).in_(
                            ("active", "degraded", "lost")
                        )
                    )
                )
            ).all()
        )

    # The log manager caches its dictionaries. Keep ledger overlays out of that cache.
    result = deepcopy(legacy_dashboard)
    if not diagnostic_available:
        result["metrics"] = {
            "metrics": [],
            "totals": {name: 0 for name in _DIAGNOSTIC_FIELDS},
        }
        summary = result.setdefault("summary", {})
        for name in (
            *_DIAGNOSTIC_FIELDS,
            "total_entries",
            "total_errors",
            "total_warnings",
            "refunds_sats",
            "success_rate",
            "refund_rate",
        ):
            summary[name] = 0
        summary["error_types"] = {}
        result["error_details"] = {"errors": [], "total_count": 0}
        result["revenue_by_model"] = {"models": []}
    totals = {name: 0 for name in _SETTLED_FIELDS}
    metrics_by_time: dict[str, dict[str, Any]] = {}
    for point in result.get("metrics", {}).get("metrics", []):
        at = datetime.fromisoformat(point["timestamp"])
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        timestamp_ms = int(at.timestamp() * 1000)
        if start_ms // bucket_ms * bucket_ms <= timestamp_ms < end_ms:
            stamp = _timestamp(timestamp_ms // bucket_ms, bucket_ms)
            metrics_by_time[stamp] = {**point, "timestamp": stamp, **totals}
    mix: dict[str, dict[str, Any]] = {}
    for row in bucket_rows:
        stamp = _timestamp(int(row.bucket), bucket_ms)
        values = _values(row)
        for name in _SETTLED_FIELDS:
            totals[name] += values[name]
        point = metrics_by_time.setdefault(
            stamp, {name: 0 for name in _DIAGNOSTIC_FIELDS}
        )
        point.update(timestamp=stamp, **values)
        mix[stamp] = {
            "timestamp": stamp,
            "total_successful": values["successful_chat_completions"],
            "total_revenue_msats": values["revenue_msats"],
            "total_tokens": values["total_tokens"],
            "others": values["successful_chat_completions"],
            "others_revenue_msats": values["revenue_msats"],
            "others_tokens": values["total_tokens"],
            "model_counts": {},
            "model_revenue_msats": {},
            "model_tokens": {},
        }
    for row in model_bucket_rows:
        point = mix[_timestamp(int(row.bucket), bucket_ms)]
        for target, value, others in (
            ("model_counts", int(row.successful_chat_completions), "others"),
            ("model_revenue_msats", int(row.revenue_msats), "others_revenue_msats"),
            ("model_tokens", int(row.total_tokens), "others_tokens"),
        ):
            point[target][str(row.model)] = value
            point[others] -= value
    metric_points = sorted(
        metrics_by_time.values(), key=lambda point: point["timestamp"]
    )
    result["metrics"] = {
        **result.get("metrics", {}),
        "metrics": metric_points,
        "interval_minutes": interval,
        "hours_back": window_hours,
        "total_buckets": len(metric_points),
        "totals": {**result.get("metrics", {}).get("totals", {}), **totals},
    }
    summary = result.setdefault("summary", {})
    completed = totals["successful_chat_completions"]
    measured_requests = int(observation["measured_token_requests"] or 0)
    measured_tokens = int(observation["measured_tokens"] or 0)
    summary.update(totals)
    summary.update(
        unique_models_count=int(model_count),
        unique_models=selected,
        unique_models_truncated=len(selected) < int(model_count),
        revenue_sats=totals["revenue_msats"] / 1000,
        # Retained for older API clients; ledger revenue never subtracts hold releases.
        net_revenue_msats=totals["revenue_msats"],
        net_revenue_sats=totals["revenue_msats"] / 1000,
        avg_input_tokens_per_completion=totals["input_tokens"] / completed
        if completed
        else 0,
        avg_output_tokens_per_completion=totals["output_tokens"] / completed
        if completed
        else 0,
        avg_total_tokens_per_completion=totals["total_tokens"] / completed
        if completed
        else 0,
        measured_token_requests=measured_requests,
        measured_tokens=measured_tokens,
        avg_measured_tokens_per_completion=measured_tokens / measured_requests
        if measured_requests
        else None,
        avg_revenue_per_request_msats=totals["revenue_msats"] / completed
        if completed
        else 0,
    )
    legacy_models = {
        row["model"]: row
        for row in result.get("revenue_by_model", {}).get("models", [])
    }
    revenue_models = []
    for row in model_rows:
        name = str(row.model) if row.model is not None else "unknown"
        previous = legacy_models.get(name, {})
        sats = int(row.revenue_msats) / 1000
        count = int(row.successful_chat_completions)
        revenue_models.append(
            {
                **previous,
                "model": name,
                "revenue_sats": sats,
                "net_revenue_sats": sats,
                "refunds_sats": previous.get("refunds_sats", 0),
                "requests": previous.get("requests", 0),
                "successful": count,
                "failed": previous.get("failed", 0),
                "avg_revenue_per_request": sats / count if count else 0,
            }
        )
    result["revenue_by_model"] = {
        "models": revenue_models,
        "total_revenue_sats": totals["revenue_msats"] / 1000,
        "total_models": int(model_count) + int(bool(observation["unattributed"])),
    }
    result["model_usage_mix"] = {
        "top_models": top_models["requests"],
        "top_models_by_metric": top_models,
        "metrics": list(mix.values()),
        "interval_minutes": interval,
        "hours_back": window_hours,
        "total_buckets": len(mix),
    }
    incomplete_days = _coverage_days(start, end, epochs, clock)
    checkpoints = [run.flushed_through_ms or run.started_at_ms for run in runs]
    # A writer always trails the clock; only a checkpoint left in an earlier day
    # is a gap. Today's buckets past the checkpoint are still updating.
    unsafe_days = [
        day
        for day in (
            datetime.fromtimestamp(checkpoint / 1000, UTC).date()
            for checkpoint in checkpoints
        )
        if day < clock.date()
    ]
    unsafe_days.extend(run.loss_day for run in runs if run.loss_day is not None)
    writer = terminal_outcomes.terminal_outcome_writer
    if writer.loss_pending and writer.loss_day is not None:
        unsafe_days.append(writer.loss_day)
    if not runs and any(epoch.current_slot == 1 for epoch in epochs):
        unsafe_days.append(clock.date())
    if unsafe_days:
        day = max(start.date(), min(unsafe_days))
        last = (end - timedelta(milliseconds=1)).date()
        while day <= last:
            incomplete_days.append(day.isoformat())
            day += timedelta(days=1)
    incomplete_days = sorted(set(incomplete_days))
    result["analytics_source"] = "terminal_outcomes"
    result["ledger_coverage"] = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "complete": not incomplete_days,
        "incomplete_days": incomplete_days,
        "includes_current_day": start.date()
        <= clock.date()
        <= (end - timedelta(milliseconds=1)).date(),
        "diagnostic_available": diagnostic_available,
        "flushed_through": datetime.fromtimestamp(
            min(checkpoints) / 1000, UTC
        ).isoformat()
        if checkpoints
        else None,
        "latest_outcome_at": datetime.fromtimestamp(
            observation["latest"] / 1000, UTC
        ).isoformat()
        if observation["latest"] is not None
        else None,
        "token_sources": {
            dimension: {
                status: int(observation[f"{dimension}_{status}"] or 0)
                for status in ("reported", "estimated", "missing")
            }
            for dimension in _DIMENSIONS
        },
    }
    first_bucket, last_bucket = start_ms // bucket_ms, (end_ms - 1) // bucket_ms
    fill_complete = last_bucket - first_bucket + 1 <= 1000
    if fill_complete:
        stamps = [
            _timestamp(value, bucket_ms)
            for value in range(first_bucket, last_bucket + 1)
        ]
    else:
        stamps = sorted(metrics_by_time.keys() | mix.keys())
    missing_days = set(incomplete_days)
    for stamp in stamps:
        bucket_start = datetime.fromisoformat(stamp).replace(tzinfo=UTC)
        day = max(start, bucket_start).date()
        last_day = (
            min(end, bucket_start + timedelta(milliseconds=bucket_ms))
            - timedelta(milliseconds=1)
        ).date()
        covered = True
        while day <= last_day:
            covered = covered and day.isoformat() not in missing_days
            day += timedelta(days=1)
        recorded = stamp in mix
        bucket_end_ms = min(end_ms, int(bucket_start.timestamp() * 1000) + bucket_ms)
        if not covered:
            coverage = "partial" if recorded else "missing"
        elif checkpoints and bucket_end_ms > min(checkpoints):
            coverage = "updating"
        else:
            coverage = "complete"
        empty = 0 if covered else None
        point = metrics_by_time.setdefault(
            stamp,
            {
                "timestamp": stamp,
                **{name: 0 for name in _DIAGNOSTIC_FIELDS},
            },
        )
        if not recorded:
            point.update({name: empty for name in _SETTLED_FIELDS})
        point["coverage"] = coverage
        model_point = mix.setdefault(
            stamp,
            {
                "timestamp": stamp,
                "total_successful": empty,
                "total_revenue_msats": empty,
                "total_tokens": empty,
                "others": empty,
                "others_revenue_msats": empty,
                "others_tokens": empty,
                "model_counts": {},
                "model_revenue_msats": {},
                "model_tokens": {},
            },
        )
        model_point["coverage"] = coverage
    for key, points in (("metrics", metrics_by_time), ("model_usage_mix", mix)):
        result[key].update(
            metrics=[points[stamp] for stamp in stamps],
            total_buckets=len(stamps),
            bucket_fill_complete=fill_complete,
        )
    return result
