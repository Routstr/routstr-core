from __future__ import annotations

from typing import Any

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


def test_build_latest_payload_contains_windows_and_v2_schema(monkeypatch: Any) -> None:
    seen_windows: set[tuple[int, int]] = set()

    def fake_usage_dashboard(
        *, interval: int, hours: int, error_limit: int, model_limit: int
    ) -> dict[str, Any]:
        seen_windows.add((hours, interval))
        assert error_limit == 1
        assert model_limit == 20
        return {
            "summary": {
                "total_requests": 20,
                "successful_chat_completions": 18,
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
            "revenue_by_model": {
                "models": [
                    {
                        "model": "openai/gpt-4o",
                        "requests": 15,
                        "successful": 14,
                        "failed": 1,
                        "revenue_sats": 7.2,
                        "refunds_sats": 0.3,
                        "net_revenue_sats": 6.9,
                    }
                ]
            },
            "model_usage_mix": {
                "top_models": ["openai/gpt-4o"],
                "metrics": [
                    {
                        "timestamp": "2026-03-02 10:00:00",
                        "model_counts": {"openai/gpt-4o": 14},
                        "model_revenue_msats": {"openai/gpt-4o": 7200.0},
                        "model_tokens": {"openai/gpt-4o": 2600},
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

    payload = analytics.build_latest_usage_analytics_payload(
        "provider123",
        public_key_hex="ab" * 32,
        generated_at=1772451600,
        model_limit=20,
    )
    assert seen_windows == {(24, 60), (7 * 24, 6 * 60), (30 * 24, 24 * 60)}
    assert payload["schema"] == analytics.ANALYTICS_SCHEMA
    assert payload["provider_id"] == "provider123"
    assert payload["period_type"] == "latest"
    assert payload["period_key"] == "latest"
    assert payload["endpoint_urls"] == ["https://node.example.com"]
    assert set(payload["windows"].keys()) == {"24h", "7d", "30d"}


def test_day_and_month_payload_keys(monkeypatch: Any) -> None:
    def fake_usage_dashboard(
        *, interval: int, hours: int, error_limit: int, model_limit: int
    ) -> dict[str, Any]:
        _ = (error_limit, model_limit)
        return {
            "summary": {
                "total_requests": max(1, hours),
                "successful_chat_completions": max(1, hours),
                "failed_requests": 0,
                "total_tokens": max(1, hours) * 100,
                "revenue_sats": float(max(1, hours)),
            },
            "revenue_by_model": {"models": []},
            "model_usage_mix": {"top_models": [], "metrics": []},
        }

    monkeypatch.setattr(
        analytics.log_manager, "get_usage_dashboard", fake_usage_dashboard
    )
    monkeypatch.setattr(analytics.settings, "npub", "npub1example")
    monkeypatch.setattr(analytics.settings, "http_url", "https://node.example.com")
    monkeypatch.setattr(analytics.settings, "onion_url", "")

    generated_at = 1772451600  # 2026-03-02
    day_payload = analytics.build_day_usage_analytics_payload(
        "provider123",
        public_key_hex="ab" * 32,
        generated_at=generated_at,
    )
    month_payload = analytics.build_month_usage_analytics_payload(
        "provider123",
        public_key_hex="ab" * 32,
        generated_at=generated_at,
    )

    assert day_payload["period_type"] == "day"
    assert day_payload["period_key"] == "2026-03-02"
    assert day_payload["day"] == "2026-03-02"
    assert month_payload["period_type"] == "month"
    assert month_payload["period_key"] == "2026-03"
    assert month_payload["month"] == "2026-03"


def test_create_usage_analytics_event_tags() -> None:
    private_key_hex = "11" * 32
    event = analytics.create_usage_analytics_event(
        private_key_hex,
        "provider123",
        payload_json='{"schema":"routstr.analytics.usage.v2"}',
        period_type="day",
        period_key="2026-03-02",
        d_tag="provider123:usage:day:2026-03-02",
    )

    tags = event["tags"]
    assert ["d", "provider123:usage:day:2026-03-02"] in tags
    assert ["provider", "provider123"] in tags
    assert ["schema", analytics.ANALYTICS_SCHEMA] in tags
    assert ["period", "day"] in tags
    assert ["period_key", "2026-03-02"] in tags
    assert ["day", "2026-03-02"] in tags


def test_checkpoint_payload_contains_chain_hash() -> None:
    payload = analytics.build_analytics_checkpoint_payload(
        "provider123",
        public_key_hex="ab" * 32,
        generated_at=1772451600,
        day_utc="2026-03-02",
        refs={
            "latest": {"d": "provider123:usage:latest", "payload_hash": "a"},
            "day": {"d": "provider123:usage:day:2026-03-02", "payload_hash": "b"},
            "month": {"d": "provider123:usage:month:2026-03", "payload_hash": "c"},
        },
        previous_checkpoint_hash="prev-hash",
    )

    assert payload["schema"] == analytics.ANALYTICS_CHECKPOINT_SCHEMA
    assert payload["day_utc"] == "2026-03-02"
    assert payload["previous_checkpoint_hash"] == "prev-hash"
    assert isinstance(payload["checkpoint_hash"], str)
    assert len(payload["checkpoint_hash"]) == 64
