#!/usr/bin/env python3
"""
Nostr usage analytics publisher.
Publishes a single replaceable analytics snapshot for each provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from nostr.event import Event
from nostr.key import PrivateKey

from ..core import get_logger
from ..core.log_manager import log_manager
from ..core.settings import settings
from .listing import nsec_to_keypair, publish_to_relay

logger = get_logger(__name__)

ANALYTICS_KIND = 38422
ANALYTICS_SCHEMA = "routstr.analytics.snapshot.v1"
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
    ("3m", 90 * 24, 24 * 60),
    ("1y", 365 * 24, 7 * 24 * 60),
)


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


def _build_window_payload(
    *,
    hours: int,
    interval_minutes: int,
    model_limit: int,
) -> dict[str, Any]:
    dashboard = log_manager.get_usage_dashboard(
        interval=interval_minutes,
        hours=hours,
        error_limit=1,
        model_limit=model_limit,
    )

    summary = dashboard.get("summary", {})
    model_usage_mix = dashboard.get("model_usage_mix", {})

    summary_payload = _build_summary_payload(summary if isinstance(summary, dict) else {})
    usage_mix_payload = model_usage_mix if isinstance(model_usage_mix, dict) else {}
    top_model_usage, others_usage = _aggregate_top_model_usage(usage_mix_payload)

    return {
        "window_hours": hours,
        "interval_minutes": interval_minutes,
        "summary": summary_payload,
        "model_usage_mix": usage_mix_payload,
        "top_model_usage": top_model_usage,
        "others_usage": others_usage,
    }


def build_stats_snapshot_payload(
    provider_id: str,
    *,
    public_key_hex: str,
    generated_at: int,
    window_hours: int = DASHBOARD_WINDOW_HOURS,
    interval_minutes: int = DASHBOARD_INTERVAL_MINUTES,
    model_limit: int = MODEL_LIMIT,
) -> dict[str, Any]:
    _ = (window_hours, interval_minutes)
    windows: dict[str, dict[str, Any]] = {}
    for key, hours, window_interval_minutes in WINDOW_DEFINITIONS:
        windows[key] = _build_window_payload(
            hours=hours,
            interval_minutes=window_interval_minutes,
            model_limit=model_limit,
        )

    primary_window = windows.get("24h", {})
    summary_payload = (
        primary_window.get("summary", {})
        if isinstance(primary_window.get("summary", {}), dict)
        else {}
    )
    usage_mix_payload = (
        primary_window.get("model_usage_mix", {})
        if isinstance(primary_window.get("model_usage_mix", {}), dict)
        else {}
    )
    top_model_usage = (
        primary_window.get("top_model_usage", [])
        if isinstance(primary_window.get("top_model_usage", []), list)
        else []
    )
    others_usage = (
        primary_window.get("others_usage", {})
        if isinstance(primary_window.get("others_usage", {}), dict)
        else {}
    )

    return {
        "schema": ANALYTICS_SCHEMA,
        "generated_at": generated_at,
        "provider_id": provider_id,
        "pubkey": public_key_hex,
        "npub": settings.npub or "",
        "endpoint_urls": _resolve_endpoint_urls(),
        "window_hours": DASHBOARD_WINDOW_HOURS,
        "interval_minutes": DASHBOARD_INTERVAL_MINUTES,
        "summary": summary_payload,
        "model_usage_mix": usage_mix_payload,
        "top_model_usage": top_model_usage,
        "others_usage": others_usage,
        "windows": windows,
    }


def create_stats_snapshot_event(
    private_key_hex: str,
    provider_id: str,
    payload_json: str,
    *,
    d_tag: str,
) -> dict[str, Any]:
    private_key = PrivateKey(bytes.fromhex(private_key_hex))
    tags = [
        ["d", d_tag],
        ["provider", provider_id],
        ["schema", ANALYTICS_SCHEMA],
    ]

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
    # Ignore generated timestamp for semantic dedupe.
    normalized.pop("generated_at", None)
    payload_json = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


async def publish_usage_analytics() -> None:
    last_payload_hash: str | None = None

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
                last_payload_hash = None

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
            payload = build_stats_snapshot_payload(
                resolved_provider_id,
                public_key_hex=public_key_hex,
                generated_at=now_ts,
            )

            payload_hash = _fingerprint_payload(payload)
            if last_payload_hash == payload_hash:
                await asyncio.sleep(PUBLISH_INTERVAL_SECONDS)
                continue

            payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            d_tag = f"{resolved_provider_id}:stats"
            event = create_stats_snapshot_event(
                private_key_hex,
                resolved_provider_id,
                payload_json,
                d_tag=d_tag,
            )

            success_count = 0
            for relay_url in relay_urls:
                if await publish_to_relay(relay_url, event):
                    success_count += 1

            if success_count > 0:
                last_payload_hash = payload_hash

            logger.info(
                "Published analytics snapshot (success=%s/%s provider=%s)",
                success_count,
                len(relay_urls),
                resolved_provider_id,
                extra={
                    "relay_success_count": success_count,
                    "relay_total": len(relay_urls),
                    "provider_id": resolved_provider_id,
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
