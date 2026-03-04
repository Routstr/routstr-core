#!/usr/bin/env python3
"""
Nostr usage analytics publisher.
Publishes routstr analytics snapshots for latest/day/month plus daily checkpoints.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypedDict

from nostr.event import Event
from nostr.key import PrivateKey

from ..core import get_logger
from ..core.log_manager import log_manager
from ..core.settings import settings
from .listing import nsec_to_keypair, publish_to_relay

logger = get_logger(__name__)

ANALYTICS_KIND = 38422
ANALYTICS_SCHEMA = "routstr.analytics.usage.v2"
ANALYTICS_CHECKPOINT_SCHEMA = "routstr.analytics.checkpoint.v1"
DEFAULT_RELAYS = [
    "wss://relay.nostr.band",
    "wss://relay.damus.io",
    "wss://relay.routstr.com",
    "wss://nos.lol",
]
PUBLISH_INTERVAL_SECONDS = 15 * 60
DISABLED_POLL_SECONDS = 60
DASHBOARD_WINDOW_HOURS = 24
DASHBOARD_INTERVAL_MINUTES = 60
MODEL_LIMIT = 20

WINDOW_DEFINITIONS: tuple[tuple[str, int, int], ...] = (
    ("24h", 24, 60),
    ("7d", 7 * 24, 6 * 60),
    ("30d", 30 * 24, 24 * 60),
)


class PayloadSpec(TypedDict):
    period_type: str
    period_key: str
    d_tag: str
    payload: dict[str, Any]


def _event_to_dict(ev: Event) -> dict[str, Any]:
    return {
        "id": ev.id,
        "pubkey": ev.public_key,
        "created_at": ev.created_at,
        "kind": int(ev.kind) if not isinstance(ev.kind, int) else ev.kind,
        "tags": ev.tags,
        "content": ev.content,
        "sig": ev.signature,
    }


def _resolve_provider_id(public_key_hex: str) -> str:
    explicit_provider_id = (settings.provider_id or "").strip()
    if explicit_provider_id:
        return explicit_provider_id
    return public_key_hex[:12]


def _resolve_endpoint_urls() -> list[str]:
    urls: list[str] = []
    http_url = (settings.http_url or "").strip()
    onion_url = (settings.onion_url or "").strip()

    if http_url and http_url != "http://localhost:8000":
        urls.append(http_url)

    if onion_url:
        if onion_url.endswith(".onion") and not (
            onion_url.startswith("http://") or onion_url.startswith("https://")
        ):
            onion_url = f"http://{onion_url}"
        urls.append(onion_url)

    return urls


def _resolve_relays() -> list[str]:
    configured = [url.strip() for url in settings.relays if url.strip()]
    return configured if configured else list(DEFAULT_RELAYS)


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _utc_day_key(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _utc_month_key(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m")


def _utc_day_start_ts(unix_ts: int) -> int:
    dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    return int(start.timestamp())


def _utc_month_start_ts(unix_ts: int) -> int:
    dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
    return int(start.timestamp())


def _hours_since(start_ts: int, end_ts: int) -> int:
    elapsed = max(1, end_ts - start_ts)
    return max(1, int(math.ceil(elapsed / 3600)))


def _aggregate_top_model_usage(
    model_usage_mix: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    top_models_raw = model_usage_mix.get("top_models", [])
    mix_metrics_raw = model_usage_mix.get("metrics", [])

    top_models = [model for model in top_models_raw if isinstance(model, str)]
    metrics = [row for row in mix_metrics_raw if isinstance(row, dict)]

    model_totals: dict[str, dict[str, float | int]] = {
        model: {
            "successful_requests": 0,
            "revenue_msats": 0.0,
            "total_tokens": 0,
        }
        for model in top_models
    }
    others = {
        "successful_requests": 0,
        "revenue_msats": 0.0,
        "total_tokens": 0,
    }

    for metric in metrics:
        model_counts = metric.get("model_counts", {})
        model_revenue = metric.get("model_revenue_msats", {})
        model_tokens = metric.get("model_tokens", {})

        if isinstance(model_counts, dict):
            for model, count in model_counts.items():
                if model in model_totals:
                    model_totals[model]["successful_requests"] += _to_int(count)

        if isinstance(model_revenue, dict):
            for model, amount in model_revenue.items():
                if model in model_totals:
                    model_totals[model]["revenue_msats"] += _to_float(amount)

        if isinstance(model_tokens, dict):
            for model, token_count in model_tokens.items():
                if model in model_totals:
                    model_totals[model]["total_tokens"] += _to_int(token_count)

        others["successful_requests"] += _to_int(metric.get("others", 0))
        others["revenue_msats"] += _to_float(metric.get("others_revenue_msats", 0.0))
        others["total_tokens"] += _to_int(metric.get("others_tokens", 0))

    model_rows = [
        {
            "model": model,
            "successful_requests": int(values["successful_requests"]),
            "revenue_msats": float(values["revenue_msats"]),
            "total_tokens": int(values["total_tokens"]),
        }
        for model, values in model_totals.items()
    ]
    model_rows.sort(
        key=lambda row: _to_int(row.get("successful_requests", 0)),
        reverse=True,
    )

    return model_rows, others


def _build_summary_payload(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_requests": _to_int(summary.get("total_requests", 0)),
        "successful_chat_completions": _to_int(
            summary.get("successful_chat_completions", 0)
        ),
        "failed_requests": _to_int(summary.get("failed_requests", 0)),
        "success_rate": _to_float(summary.get("success_rate", 0.0)),
        "unique_models_count": _to_int(summary.get("unique_models_count", 0)),
        "input_tokens": _to_int(summary.get("input_tokens", 0)),
        "output_tokens": _to_int(summary.get("output_tokens", 0)),
        "total_tokens": _to_int(summary.get("total_tokens", 0)),
        "revenue_msats": _to_float(summary.get("revenue_msats", 0.0)),
        "refunds_msats": _to_float(summary.get("refunds_msats", 0.0)),
        "net_revenue_msats": _to_float(summary.get("net_revenue_msats", 0.0)),
        "revenue_sats": _to_float(summary.get("revenue_sats", 0.0)),
        "refunds_sats": _to_float(summary.get("refunds_sats", 0.0)),
        "net_revenue_sats": _to_float(summary.get("net_revenue_sats", 0.0)),
    }


def _build_model_revenue_rows(revenue_by_model: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    models_raw = revenue_by_model.get("models", [])
    if not isinstance(models_raw, list):
        return rows

    for row in models_raw:
        if not isinstance(row, dict):
            continue
        model_name = str(row.get("model", "unknown"))
        rows.append(
            {
                "model": model_name,
                "requests": _to_int(row.get("requests", 0)),
                "successful": _to_int(row.get("successful", 0)),
                "failed": _to_int(row.get("failed", 0)),
                "revenue_sats": _to_float(row.get("revenue_sats", 0.0)),
                "refunds_sats": _to_float(row.get("refunds_sats", 0.0)),
                "net_revenue_sats": _to_float(row.get("net_revenue_sats", 0.0)),
            }
        )
    return rows


def _build_window_payload(
    *,
    hours: int,
    interval: int,
    model_limit: int,
) -> dict[str, Any]:
    dashboard = log_manager.get_usage_dashboard(
        interval=interval,
        hours=hours,
        error_limit=1,
        model_limit=model_limit,
    )
    return _build_window_payload_from_dashboard(
        dashboard=dashboard,
        hours=hours,
        interval=interval,
    )


def _build_window_payload_from_dashboard(
    *,
    dashboard: dict[str, Any],
    hours: int,
    interval: int,
) -> dict[str, Any]:
    summary = dashboard.get("summary", {})
    revenue_by_model = dashboard.get("revenue_by_model", {})
    model_usage_mix = dashboard.get("model_usage_mix", {})

    summary_payload = _build_summary_payload(summary if isinstance(summary, dict) else {})
    model_revenue_rows = _build_model_revenue_rows(
        revenue_by_model if isinstance(revenue_by_model, dict) else {}
    )
    usage_mix_payload = model_usage_mix if isinstance(model_usage_mix, dict) else {}
    top_model_usage, others_usage = _aggregate_top_model_usage(usage_mix_payload)

    return {
        "window_hours": hours,
        "interval_minutes": interval,
        "summary": summary_payload,
        "model_revenue": model_revenue_rows,
        "top_model_usage": top_model_usage,
        "others_usage": others_usage,
        "model_usage_mix": usage_mix_payload,
    }


def _build_window_payload_for_range(
    *,
    start_unix: int,
    end_unix: int,
    interval: int,
    model_limit: int,
) -> dict[str, Any]:
    safe_start = max(0, int(start_unix))
    safe_end = max(safe_start + 60, int(end_unix))
    hours = _hours_since(safe_start, safe_end)

    # Prefer exact range query when supported.
    try:
        dashboard = log_manager.get_usage_dashboard_range(
            interval=interval,
            start_unix=safe_start,
            end_unix=safe_end,
            error_limit=1,
            model_limit=model_limit,
        )
    except Exception:
        dashboard = log_manager.get_usage_dashboard(
            interval=interval,
            hours=hours,
            error_limit=1,
            model_limit=model_limit,
        )

    return _build_window_payload_from_dashboard(
        dashboard=dashboard,
        hours=hours,
        interval=interval,
    )


def build_latest_usage_analytics_payload(
    provider_id: str,
    *,
    public_key_hex: str,
    generated_at: int,
    model_limit: int = MODEL_LIMIT,
) -> dict[str, Any]:
    windows: dict[str, dict[str, Any]] = {}
    for key, window_hours, window_interval in WINDOW_DEFINITIONS:
        windows[key] = _build_window_payload(
            hours=window_hours,
            interval=window_interval,
            model_limit=model_limit,
        )

    primary_window = windows.get("24h", {})
    return {
        "schema": ANALYTICS_SCHEMA,
        "generated_at": generated_at,
        "provider_id": provider_id,
        "pubkey": public_key_hex,
        "npub": settings.npub or "",
        "endpoint_urls": _resolve_endpoint_urls(),
        "period_type": "latest",
        "period_key": "latest",
        "period_start_unix": generated_at - (24 * 3600),
        "period_end_unix": generated_at,
        "summary": primary_window.get("summary", {}),
        "model_revenue": primary_window.get("model_revenue", []),
        "top_model_usage": primary_window.get("top_model_usage", []),
        "others_usage": primary_window.get("others_usage", {}),
        "model_usage_mix": primary_window.get("model_usage_mix", {}),
        "windows": windows,
    }


def _build_period_payload(
    provider_id: str,
    *,
    public_key_hex: str,
    generated_at: int,
    period_type: str,
    period_key: str,
    period_start_unix: int,
    interval_minutes: int,
    model_limit: int = MODEL_LIMIT,
) -> dict[str, Any]:
    hours = _hours_since(period_start_unix, generated_at)
    window = _build_window_payload(
        hours=hours,
        interval=interval_minutes,
        model_limit=model_limit,
    )
    return {
        "schema": ANALYTICS_SCHEMA,
        "generated_at": generated_at,
        "provider_id": provider_id,
        "pubkey": public_key_hex,
        "npub": settings.npub or "",
        "endpoint_urls": _resolve_endpoint_urls(),
        "period_type": period_type,
        "period_key": period_key,
        "period_start_unix": period_start_unix,
        "period_end_unix": generated_at,
        "window_hours": window.get("window_hours", hours),
        "interval_minutes": window.get("interval_minutes", interval_minutes),
        "summary": window.get("summary", {}),
        "model_revenue": window.get("model_revenue", []),
        "top_model_usage": window.get("top_model_usage", []),
        "others_usage": window.get("others_usage", {}),
        "model_usage_mix": window.get("model_usage_mix", {}),
    }


def build_day_usage_analytics_payload(
    provider_id: str,
    *,
    public_key_hex: str,
    generated_at: int,
    model_limit: int = MODEL_LIMIT,
) -> dict[str, Any]:
    day_key = _utc_day_key(generated_at)
    day_start = _utc_day_start_ts(generated_at)
    payload = _build_period_payload(
        provider_id,
        public_key_hex=public_key_hex,
        generated_at=generated_at,
        period_type="day",
        period_key=day_key,
        period_start_unix=day_start,
        interval_minutes=60,
        model_limit=model_limit,
    )
    payload["day"] = day_key
    return payload


def build_month_usage_analytics_payload(
    provider_id: str,
    *,
    public_key_hex: str,
    generated_at: int,
    model_limit: int = MODEL_LIMIT,
) -> dict[str, Any]:
    month_key = _utc_month_key(generated_at)
    month_start = _utc_month_start_ts(generated_at)
    payload = _build_period_payload(
        provider_id,
        public_key_hex=public_key_hex,
        generated_at=generated_at,
        period_type="month",
        period_key=month_key,
        period_start_unix=month_start,
        interval_minutes=24 * 60,
        model_limit=model_limit,
    )
    payload["month"] = month_key
    return payload


def _build_period_payload_for_range(
    provider_id: str,
    *,
    public_key_hex: str,
    generated_at: int,
    period_type: str,
    period_key: str,
    period_start_unix: int,
    period_end_unix: int,
    interval_minutes: int,
    model_limit: int = MODEL_LIMIT,
) -> dict[str, Any]:
    window = _build_window_payload_for_range(
        start_unix=period_start_unix,
        end_unix=period_end_unix,
        interval=interval_minutes,
        model_limit=model_limit,
    )
    payload = {
        "schema": ANALYTICS_SCHEMA,
        "generated_at": int(generated_at),
        "provider_id": provider_id,
        "pubkey": public_key_hex,
        "npub": settings.npub or "",
        "endpoint_urls": _resolve_endpoint_urls(),
        "period_type": period_type,
        "period_key": period_key,
        "period_start_unix": int(period_start_unix),
        "period_end_unix": int(period_end_unix),
        "window_hours": window.get("window_hours", _hours_since(period_start_unix, period_end_unix)),
        "interval_minutes": window.get("interval_minutes", interval_minutes),
        "summary": window.get("summary", {}),
        "model_revenue": window.get("model_revenue", []),
        "top_model_usage": window.get("top_model_usage", []),
        "others_usage": window.get("others_usage", {}),
        "model_usage_mix": window.get("model_usage_mix", {}),
    }
    if period_type == "day":
        payload["day"] = period_key
    elif period_type == "month":
        payload["month"] = period_key
    return payload


def build_usage_analytics_payload(
    provider_id: str,
    *,
    public_key_hex: str,
    hours: int = DASHBOARD_WINDOW_HOURS,
    interval: int = DASHBOARD_INTERVAL_MINUTES,
    model_limit: int = MODEL_LIMIT,
) -> dict[str, Any]:
    # Backward-compatible helper kept for existing tests/callers.
    _ = (hours, interval)
    return build_latest_usage_analytics_payload(
        provider_id,
        public_key_hex=public_key_hex,
        generated_at=int(time.time()),
        model_limit=model_limit,
    )


def create_usage_analytics_event(
    private_key_hex: str,
    provider_id: str,
    payload_json: str,
    *,
    period_type: str,
    period_key: str,
    d_tag: str,
) -> dict[str, Any]:
    private_key = PrivateKey(bytes.fromhex(private_key_hex))
    tags = [
        ["d", d_tag],
        ["provider", provider_id],
        ["schema", ANALYTICS_SCHEMA],
        ["period", period_type],
        ["period_key", period_key],
    ]
    if period_type == "day":
        tags.append(["day", period_key])
    elif period_type == "month":
        tags.append(["month", period_key])

    event = Event(
        public_key=private_key.public_key.hex(),
        content=payload_json,
        kind=ANALYTICS_KIND,
        tags=tags,
    )
    private_key.sign_event(event)
    return _event_to_dict(event)


def _fingerprint_payload(payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    # Ignore volatile timestamps for deduping semantically identical snapshots.
    normalized.pop("generated_at", None)
    normalized.pop("period_end_unix", None)
    payload_json = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _stable_hash(data: dict[str, Any]) -> str:
    encoded = json.dumps(data, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_analytics_checkpoint_payload(
    provider_id: str,
    *,
    public_key_hex: str,
    generated_at: int,
    day_utc: str,
    refs: dict[str, dict[str, str]],
    previous_checkpoint_hash: str | None,
) -> dict[str, Any]:
    base = {
        "schema": ANALYTICS_CHECKPOINT_SCHEMA,
        "generated_at": generated_at,
        "provider_id": provider_id,
        "pubkey": public_key_hex,
        "npub": settings.npub or "",
        "day_utc": day_utc,
        "refs": refs,
        "previous_checkpoint_hash": previous_checkpoint_hash or "",
    }
    checkpoint_hash = _stable_hash(
        {
            "provider_id": provider_id,
            "day_utc": day_utc,
            "refs": refs,
            "previous_checkpoint_hash": previous_checkpoint_hash or "",
        }
    )
    base["checkpoint_hash"] = checkpoint_hash
    return base


def create_analytics_checkpoint_event(
    private_key_hex: str,
    provider_id: str,
    payload_json: str,
    *,
    day_utc: str,
    previous_checkpoint_hash: str | None,
) -> dict[str, Any]:
    private_key = PrivateKey(bytes.fromhex(private_key_hex))
    tags = [
        ["d", f"{provider_id}:usage:checkpoint:{day_utc}"],
        ["provider", provider_id],
        ["schema", ANALYTICS_CHECKPOINT_SCHEMA],
        ["day", day_utc],
    ]
    if previous_checkpoint_hash:
        tags.append(["prev", previous_checkpoint_hash])

    event = Event(
        public_key=private_key.public_key.hex(),
        content=payload_json,
        kind=ANALYTICS_KIND,
        tags=tags,
    )
    private_key.sign_event(event)
    return _event_to_dict(event)


def _utc_day_start_unix(day_value: date) -> int:
    return int(
        datetime(
            day_value.year,
            day_value.month,
            day_value.day,
            tzinfo=timezone.utc,
        ).timestamp()
    )


def _first_day_of_month(day_value: date) -> date:
    return date(day_value.year, day_value.month, 1)


def _next_month(day_value: date) -> date:
    if day_value.month == 12:
        return date(day_value.year + 1, 1, 1)
    return date(day_value.year, day_value.month + 1, 1)


def _iter_days(start_day: date, end_day: date) -> list[date]:
    days: list[date] = []
    cursor = start_day
    while cursor <= end_day:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _iter_month_starts(start_day: date, end_day: date) -> list[date]:
    months: list[date] = []
    cursor = _first_day_of_month(start_day)
    limit = _first_day_of_month(end_day)
    while cursor <= limit:
        months.append(cursor)
        cursor = _next_month(cursor)
    return months


def _parse_day_arg(value: str, *, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}: {value}. Use YYYY-MM-DD.") from exc


def _resolve_backfill_range(
    start_day: str | None,
    end_day: str | None,
) -> tuple[date, date]:
    bounds = log_manager.get_usage_time_bounds()
    min_unix = bounds.get("min_unix")
    max_unix = bounds.get("max_unix")
    inferred_start = (
        datetime.fromtimestamp(int(min_unix), tz=timezone.utc).date()
        if isinstance(min_unix, int)
        else None
    )
    inferred_end = (
        datetime.fromtimestamp(int(max_unix), tz=timezone.utc).date()
        if isinstance(max_unix, int)
        else None
    )

    resolved_start = (
        _parse_day_arg(start_day, field_name="start-day")
        if start_day
        else inferred_start
    )
    resolved_end = (
        _parse_day_arg(end_day, field_name="end-day") if end_day else inferred_end
    )
    if resolved_start is None or resolved_end is None:
        raise ValueError(
            "Could not infer backfill range from analytics index. "
            "Pass --start-day and --end-day explicitly."
        )
    if resolved_end < resolved_start:
        raise ValueError("end-day must be on or after start-day.")
    return (resolved_start, resolved_end)


async def publish_usage_analytics_backfill(
    *,
    start_day: date,
    end_day: date,
    publish_days: bool = True,
    publish_months: bool = True,
    model_limit: int = MODEL_LIMIT,
    dry_run: bool = False,
) -> dict[str, Any]:
    nsec = (settings.nsec or "").strip()
    if not nsec:
        raise ValueError("NSEC is not configured.")
    keypair = nsec_to_keypair(nsec)
    if not keypair:
        raise ValueError("Invalid NSEC.")

    private_key_hex, public_key_hex = keypair
    provider_id = _resolve_provider_id(public_key_hex)
    relay_urls = _resolve_relays()
    if not relay_urls:
        raise ValueError("No Nostr relays configured.")

    day_specs: list[PayloadSpec] = []
    if publish_days:
        for day_value in _iter_days(start_day, end_day):
            day_key = day_value.isoformat()
            day_start_unix = _utc_day_start_unix(day_value)
            day_end_unix = day_start_unix + 24 * 3600
            payload = _build_period_payload_for_range(
                provider_id,
                public_key_hex=public_key_hex,
                generated_at=day_end_unix,
                period_type="day",
                period_key=day_key,
                period_start_unix=day_start_unix,
                period_end_unix=day_end_unix,
                interval_minutes=60,
                model_limit=model_limit,
            )
            day_specs.append(
                {
                    "period_type": "day",
                    "period_key": day_key,
                    "d_tag": f"{provider_id}:usage:day:{day_key}",
                    "payload": payload,
                }
            )

    month_specs: list[PayloadSpec] = []
    if publish_months:
        end_exclusive_unix = _utc_day_start_unix(end_day + timedelta(days=1))
        for month_start in _iter_month_starts(start_day, end_day):
            month_key = month_start.strftime("%Y-%m")
            month_start_unix = _utc_day_start_unix(month_start)
            month_end_unix = _utc_day_start_unix(_next_month(month_start))
            period_end_unix = min(month_end_unix, end_exclusive_unix)
            if period_end_unix <= month_start_unix:
                continue
            payload = _build_period_payload_for_range(
                provider_id,
                public_key_hex=public_key_hex,
                generated_at=period_end_unix,
                period_type="month",
                period_key=month_key,
                period_start_unix=month_start_unix,
                period_end_unix=period_end_unix,
                interval_minutes=24 * 60,
                model_limit=model_limit,
            )
            month_specs.append(
                {
                    "period_type": "month",
                    "period_key": month_key,
                    "d_tag": f"{provider_id}:usage:month:{month_key}",
                    "payload": payload,
                }
            )

    specs = [*day_specs, *month_specs]
    if not specs:
        return {
            "provider_id": provider_id,
            "relay_total": len(relay_urls),
            "published": 0,
            "failed": 0,
            "day_events": 0,
            "month_events": 0,
            "dry_run": dry_run,
        }

    published = 0
    failed = 0
    for spec in specs:
        payload_json = json.dumps(spec["payload"], separators=(",", ":"), sort_keys=True)
        if dry_run:
            logger.info(
                "Backfill dry-run prepared %s %s (%s)",
                spec["period_type"],
                spec["period_key"],
                spec["d_tag"],
            )
            published += 1
            continue

        event = create_usage_analytics_event(
            private_key_hex,
            provider_id,
            payload_json,
            period_type=spec["period_type"],
            period_key=spec["period_key"],
            d_tag=spec["d_tag"],
        )
        success_count = 0
        for relay_url in relay_urls:
            if await publish_to_relay(relay_url, event):
                success_count += 1
        if success_count > 0:
            published += 1
        else:
            failed += 1

    return {
        "provider_id": provider_id,
        "relay_total": len(relay_urls),
        "published": published,
        "failed": failed,
        "day_events": len(day_specs),
        "month_events": len(month_specs),
        "dry_run": dry_run,
        "start_day": start_day.isoformat(),
        "end_day": end_day.isoformat(),
    }


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m routstr.nostr.analytics",
        description="Routstr analytics publisher utilities.",
    )
    subparsers = parser.add_subparsers(dest="command")

    backfill = subparsers.add_parser(
        "backfill",
        help="Publish historical day/month analytics events from local log index.",
    )
    backfill.add_argument(
        "--start-day",
        type=str,
        default=None,
        help="UTC start day (YYYY-MM-DD). Default: earliest indexed day.",
    )
    backfill.add_argument(
        "--end-day",
        type=str,
        default=None,
        help="UTC end day (YYYY-MM-DD). Default: latest indexed day.",
    )
    backfill.add_argument(
        "--skip-days",
        action="store_true",
        help="Skip day event backfill.",
    )
    backfill.add_argument(
        "--skip-months",
        action="store_true",
        help="Skip month event backfill.",
    )
    backfill.add_argument(
        "--model-limit",
        type=int,
        default=MODEL_LIMIT,
        help=f"Top models to include per payload (default: {MODEL_LIMIT}).",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="Build payloads and print summary without publishing.",
    )
    return parser


async def _run_cli_async(args: argparse.Namespace) -> int:
    if args.command != "backfill":
        return 1

    start_day, end_day = _resolve_backfill_range(args.start_day, args.end_day)
    result = await publish_usage_analytics_backfill(
        start_day=start_day,
        end_day=end_day,
        publish_days=not bool(args.skip_days),
        publish_months=not bool(args.skip_months),
        model_limit=max(1, int(args.model_limit)),
        dry_run=bool(args.dry_run),
    )
    logger.info(
        "Backfill finished (provider=%s published=%s failed=%s day_events=%s month_events=%s relays=%s dry_run=%s range=%s..%s)",
        result.get("provider_id"),
        result.get("published"),
        result.get("failed"),
        result.get("day_events"),
        result.get("month_events"),
        result.get("relay_total"),
        result.get("dry_run"),
        result.get("start_day"),
        result.get("end_day"),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1
    try:
        return asyncio.run(_run_cli_async(args))
    except ValueError as exc:
        logger.error(str(exc))
        return 2


async def publish_usage_analytics() -> None:
    last_period_state: dict[str, tuple[str, str]] = {}
    last_checkpoint_state: tuple[str, str] | None = None
    checkpoint_day: str | None = None
    checkpoint_hash_for_day: str | None = None
    previous_checkpoint_hash: str | None = None

    parsed_nsec: str | None = None
    private_key_hex: str | None = None
    public_key_hex: str | None = None
    provider_id: str | None = None
    warned_missing_nsec = False

    logger.info("Usage analytics sharing task started")

    while True:
        try:
            if not settings.enable_analytics_sharing:
                await asyncio.sleep(DISABLED_POLL_SECONDS)
                continue

            nsec = (settings.nsec or "").strip()
            if not nsec:
                if not warned_missing_nsec:
                    logger.info("NSEC is not configured; skipping analytics sharing to Nostr")
                    warned_missing_nsec = True
                await asyncio.sleep(DISABLED_POLL_SECONDS)
                continue

            warned_missing_nsec = False
            if nsec != parsed_nsec or private_key_hex is None or public_key_hex is None:
                keypair = nsec_to_keypair(nsec)
                if not keypair:
                    logger.error("Invalid NSEC; analytics sharing is paused")
                    await asyncio.sleep(DISABLED_POLL_SECONDS)
                    continue
                private_key_hex, public_key_hex = keypair
                parsed_nsec = nsec
                provider_id = _resolve_provider_id(public_key_hex)
                last_period_state = {}
                last_checkpoint_state = None
                checkpoint_day = None
                checkpoint_hash_for_day = None
                previous_checkpoint_hash = None

            if private_key_hex is None or public_key_hex is None:
                await asyncio.sleep(DISABLED_POLL_SECONDS)
                continue

            relay_urls = _resolve_relays()
            if not relay_urls:
                logger.warning("No Nostr relays configured; analytics sharing skipped")
                await asyncio.sleep(DISABLED_POLL_SECONDS)
                continue

            resolved_provider_id = provider_id or _resolve_provider_id(public_key_hex)
            now_ts = int(time.time())
            day_key = _utc_day_key(now_ts)
            month_key = _utc_month_key(now_ts)

            latest_payload = build_latest_usage_analytics_payload(
                resolved_provider_id,
                public_key_hex=public_key_hex,
                generated_at=now_ts,
            )
            day_payload = build_day_usage_analytics_payload(
                resolved_provider_id,
                public_key_hex=public_key_hex,
                generated_at=now_ts,
            )
            month_payload = build_month_usage_analytics_payload(
                resolved_provider_id,
                public_key_hex=public_key_hex,
                generated_at=now_ts,
            )

            payload_specs: list[PayloadSpec] = [
                {
                    "period_type": "latest",
                    "period_key": "latest",
                    "d_tag": f"{resolved_provider_id}:usage:latest",
                    "payload": latest_payload,
                },
                {
                    "period_type": "day",
                    "period_key": day_key,
                    "d_tag": f"{resolved_provider_id}:usage:day:{day_key}",
                    "payload": day_payload,
                },
                {
                    "period_type": "month",
                    "period_key": month_key,
                    "d_tag": f"{resolved_provider_id}:usage:month:{month_key}",
                    "payload": month_payload,
                },
            ]

            to_publish: list[dict[str, Any]] = []
            refs: dict[str, dict[str, str]] = {}
            for spec in payload_specs:
                payload: dict[str, Any] = spec["payload"]
                payload_hash = _fingerprint_payload(payload)
                d_tag = str(spec["d_tag"])
                period_type = str(spec["period_type"])

                refs[period_type] = {"d": d_tag, "payload_hash": payload_hash}
                last_state = last_period_state.get(period_type)
                if last_state is not None and last_state[0] == d_tag and last_state[1] == payload_hash:
                    continue

                payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
                event = create_usage_analytics_event(
                    private_key_hex,
                    resolved_provider_id,
                    payload_json,
                    period_type=period_type,
                    period_key=str(spec["period_key"]),
                    d_tag=d_tag,
                )
                to_publish.append(
                    {
                        "period_type": period_type,
                        "d_tag": d_tag,
                        "payload_hash": payload_hash,
                        "event": event,
                    }
                )

            period_attempted = {str(item["period_type"]) for item in to_publish}
            period_successes = {period_type: 0 for period_type in period_attempted}
            if to_publish:
                for relay_url in relay_urls:
                    for item in to_publish:
                        if await publish_to_relay(relay_url, item["event"]):
                            period_successes[item["period_type"]] += 1

                for item in to_publish:
                    period_type = item["period_type"]
                    if period_successes.get(period_type, 0) > 0:
                        last_period_state[period_type] = (
                            item["d_tag"],
                            item["payload_hash"],
                        )

            if checkpoint_day is None:
                checkpoint_day = day_key
            elif checkpoint_day != day_key:
                if checkpoint_hash_for_day:
                    previous_checkpoint_hash = checkpoint_hash_for_day
                checkpoint_day = day_key
                checkpoint_hash_for_day = None
                last_checkpoint_state = None

            checkpoint_payload = build_analytics_checkpoint_payload(
                resolved_provider_id,
                public_key_hex=public_key_hex,
                generated_at=now_ts,
                day_utc=day_key,
                refs=refs,
                previous_checkpoint_hash=previous_checkpoint_hash,
            )
            checkpoint_d = f"{resolved_provider_id}:usage:checkpoint:{day_key}"
            checkpoint_hash = _fingerprint_payload(checkpoint_payload)

            checkpoint_attempted = False
            checkpoint_success_count = 0
            if (
                last_checkpoint_state is None
                or last_checkpoint_state[0] != checkpoint_d
                or last_checkpoint_state[1] != checkpoint_hash
            ):
                checkpoint_attempted = True
                checkpoint_payload_json = json.dumps(
                    checkpoint_payload,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                checkpoint_event = create_analytics_checkpoint_event(
                    private_key_hex,
                    resolved_provider_id,
                    checkpoint_payload_json,
                    day_utc=day_key,
                    previous_checkpoint_hash=previous_checkpoint_hash,
                )
                for relay_url in relay_urls:
                    if await publish_to_relay(relay_url, checkpoint_event):
                        checkpoint_success_count += 1

                if checkpoint_success_count > 0:
                    last_checkpoint_state = (checkpoint_d, checkpoint_hash)
                    checkpoint_hash_for_day = str(
                        checkpoint_payload.get("checkpoint_hash", "")
                    ) or None

            relay_total = len(relay_urls)
            latest_result = (
                f"{period_successes.get('latest', 0)}/{relay_total}"
                if "latest" in period_attempted
                else "skip"
            )
            day_result = (
                f"{period_successes.get('day', 0)}/{relay_total}"
                if "day" in period_attempted
                else "skip"
            )
            month_result = (
                f"{period_successes.get('month', 0)}/{relay_total}"
                if "month" in period_attempted
                else "skip"
            )
            checkpoint_result = (
                f"{checkpoint_success_count}/{relay_total}"
                if checkpoint_attempted
                else "skip"
            )
            logger.info(
                "Published analytics snapshots "
                "(latest=%s day=%s month=%s checkpoint=%s day_utc=%s month_utc=%s)",
                latest_result,
                day_result,
                month_result,
                checkpoint_result,
                day_key,
                month_key,
                extra={
                    "latest_relays": period_successes.get("latest", 0),
                    "day_relays": period_successes.get("day", 0),
                    "month_relays": period_successes.get("month", 0),
                    "checkpoint_relays": checkpoint_success_count,
                    "relay_total": relay_total,
                    "day": day_key,
                    "month": month_key,
                },
            )
            await asyncio.sleep(PUBLISH_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            logger.info("Usage analytics sharing task cancelled")
            break
        except Exception as e:
            logger.error(
                "Usage analytics sharing error",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            await asyncio.sleep(DISABLED_POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
