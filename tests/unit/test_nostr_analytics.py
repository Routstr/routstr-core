from __future__ import annotations

import asyncio
from typing import Any

import pytest

from routstr.nostr import analytics


def test_aggregate_top_model_usage_sums_metrics() -> None:
    model_usage_mix = {
        "top_models": ["openai/gpt-4o", "anthropic/claude-3.5-sonnet"],
        "metrics": [
            {
                "model_counts": {
                    "openai/gpt-4o": 4,
                    "anthropic/claude-3.5-sonnet": 2,
                },
                "model_revenue_msats": {
                    "openai/gpt-4o": 1500,
                    "anthropic/claude-3.5-sonnet": 700,
                },
                "model_tokens": {
                    "openai/gpt-4o": 1200,
                    "anthropic/claude-3.5-sonnet": 600,
                },
                "others": 1,
                "others_revenue_msats": 300,
                "others_tokens": 200,
            },
            {
                "model_counts": {
                    "openai/gpt-4o": 3,
                    "anthropic/claude-3.5-sonnet": 1,
                },
                "model_revenue_msats": {
                    "openai/gpt-4o": 1000,
                    "anthropic/claude-3.5-sonnet": 500,
                },
                "model_tokens": {
                    "openai/gpt-4o": 800,
                    "anthropic/claude-3.5-sonnet": 300,
                },
                "others": 2,
                "others_revenue_msats": 450,
                "others_tokens": 350,
            },
        ],
    }

    rows, others = analytics._aggregate_top_model_usage(model_usage_mix)
    assert rows == [
        {
            "model": "openai/gpt-4o",
            "successful_requests": 7,
            "revenue_msats": 2500.0,
            "total_tokens": 2000,
        },
        {
            "model": "anthropic/claude-3.5-sonnet",
            "successful_requests": 3,
            "revenue_msats": 1200.0,
            "total_tokens": 900,
        },
    ]
    assert others == {
        "successful_requests": 3,
        "revenue_msats": 750.0,
        "total_tokens": 550,
    }


def test_build_stats_snapshot_payload_schema_and_shape(monkeypatch: Any) -> None:
    seen_windows: set[tuple[int, int]] = set()

    def fake_usage_dashboard(
        *, interval: int, hours: int, error_limit: int, model_limit: int
    ) -> dict[str, Any]:
        seen_windows.add((hours, interval))
        assert error_limit == 1
        assert model_limit == 20
        return {
            "summary": {
                "total_requests": hours,
                "successful_chat_completions": max(1, hours - 1),
                "failed_requests": 2,
                "success_rate": 90.0,
                "unique_models_count": 2,
                "input_tokens": 2000,
                "output_tokens": 1000,
                "total_tokens": 3000,
                "revenue_msats": 9000.0,
                "refunds_msats": 1000.0,
                "net_revenue_msats": 8000.0,
                "revenue_sats": 9.0,
                "refunds_sats": 1.0,
                "net_revenue_sats": 8.0,
            },
            "model_usage_mix": {
                "top_models": ["openai/gpt-4o"],
                "metrics": [
                    {
                        "timestamp": "2026-03-02 10:00:00",
                        "model_counts": {"openai/gpt-4o": hours},
                        "model_revenue_msats": {"openai/gpt-4o": float(hours * 100)},
                        "model_tokens": {"openai/gpt-4o": hours * 10},
                        "others": 4,
                        "others_revenue_msats": 1800.0,
                        "others_tokens": 400,
                    }
                ],
            },
        }

    monkeypatch.setattr(
        analytics.log_manager, "get_usage_dashboard", fake_usage_dashboard
    )
    monkeypatch.setattr(analytics.settings, "npub", "npub1example")
    monkeypatch.setattr(analytics.settings, "http_url", "https://node.example.com")
    monkeypatch.setattr(analytics.settings, "onion_url", "")

    payload = analytics.build_stats_snapshot_payload(
        "provider123",
        public_key_hex="ab" * 32,
        generated_at=1772451600,
    )

    assert payload["schema"] == analytics.ANALYTICS_SCHEMA
    assert payload["provider_id"] == "provider123"
    assert payload["window_hours"] == 24
    assert payload["interval_minutes"] == 60
    assert payload["endpoint_urls"] == ["https://node.example.com"]
    assert seen_windows == {
        (24, 60),
        (7 * 24, 6 * 60),
        (30 * 24, 24 * 60),
        (90 * 24, 24 * 60),
        (365 * 24, 7 * 24 * 60),
    }
    assert set(payload["windows"].keys()) == {"24h", "7d", "30d", "3m", "1y"}
    assert payload["windows"]["1y"]["interval_minutes"] == 7 * 24 * 60
    assert payload["summary"]["total_requests"] == 24
    assert payload["top_model_usage"] == [
        {
            "model": "openai/gpt-4o",
            "successful_requests": 24,
            "revenue_msats": 2400.0,
            "total_tokens": 240,
        }
    ]
    assert payload["others_usage"] == {
        "successful_requests": 4,
        "revenue_msats": 1800.0,
        "total_tokens": 400,
    }


def test_create_stats_snapshot_event_tags() -> None:
    private_key_hex = "11" * 32
    event = analytics.create_stats_snapshot_event(
        private_key_hex,
        "provider123",
        payload_json='{"schema":"routstr.analytics.snapshot.v1"}',
        d_tag="provider123:stats",
    )

    tags = event["tags"]
    assert ["d", "provider123:stats"] in tags
    assert ["provider", "provider123"] in tags
    assert ["schema", analytics.ANALYTICS_SCHEMA] in tags
    assert all(tag[0] != "period" for tag in tags)


def test_fingerprint_payload_ignores_generated_at() -> None:
    a = {"schema": analytics.ANALYTICS_SCHEMA, "generated_at": 1000, "summary": {"x": 1}}
    b = {"schema": analytics.ANALYTICS_SCHEMA, "generated_at": 2000, "summary": {"x": 1}}

    assert analytics._fingerprint_payload(a) == analytics._fingerprint_payload(b)


@pytest.mark.asyncio
async def test_publish_usage_analytics_skips_when_disabled(monkeypatch: Any) -> None:
    delays: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        delays.append(seconds)
        raise asyncio.CancelledError()

    def fail_build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("build_stats_snapshot_payload should not be called")

    monkeypatch.setattr(analytics.settings, "enable_analytics_sharing", False)
    monkeypatch.setattr(analytics, "build_stats_snapshot_payload", fail_build)
    monkeypatch.setattr(analytics.asyncio, "sleep", fake_sleep)

    await analytics.publish_usage_analytics()

    assert delays == [analytics.DISABLED_POLL_SECONDS]


@pytest.mark.asyncio
async def test_publish_usage_analytics_skips_without_nsec(monkeypatch: Any) -> None:
    delays: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        delays.append(seconds)
        raise asyncio.CancelledError()

    def fail_build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("build_stats_snapshot_payload should not be called")

    monkeypatch.setattr(analytics.settings, "enable_analytics_sharing", True)
    monkeypatch.setattr(analytics.settings, "nsec", "")
    monkeypatch.setattr(analytics, "build_stats_snapshot_payload", fail_build)
    monkeypatch.setattr(analytics.asyncio, "sleep", fake_sleep)

    await analytics.publish_usage_analytics()

    assert delays == [analytics.DISABLED_POLL_SECONDS]


@pytest.mark.asyncio
async def test_publish_usage_analytics_dedupes_unchanged_payload(monkeypatch: Any) -> None:
    published_events: list[dict[str, Any]] = []
    sleep_calls = 0

    async def fake_sleep(seconds: int) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError()

    def fake_build_payload(
        provider_id: str,
        *,
        public_key_hex: str,
        generated_at: int,
        window_hours: int = 24,
        interval_minutes: int = 60,
        model_limit: int = 10,
    ) -> dict[str, Any]:
        _ = (public_key_hex, generated_at, window_hours, interval_minutes, model_limit)
        return {
            "schema": analytics.ANALYTICS_SCHEMA,
            "generated_at": generated_at,
            "provider_id": provider_id,
            "summary": {"total_requests": 1},
        }

    async def fake_publish(relay_url: str, event: dict[str, Any]) -> bool:
        _ = relay_url
        published_events.append(event)
        return True

    monkeypatch.setattr(analytics.settings, "enable_analytics_sharing", True)
    monkeypatch.setattr(analytics.settings, "nsec", "11" * 32)
    monkeypatch.setattr(analytics.settings, "relays", ["wss://relay.example.com"])
    monkeypatch.setattr(analytics.settings, "provider_id", "")
    monkeypatch.setattr(analytics, "build_stats_snapshot_payload", fake_build_payload)
    monkeypatch.setattr(analytics, "publish_to_relay", fake_publish)
    monkeypatch.setattr(analytics.asyncio, "sleep", fake_sleep)

    await analytics.publish_usage_analytics()

    assert len(published_events) == 1
    assert ["schema", analytics.ANALYTICS_SCHEMA] in published_events[0].get("tags", [])
