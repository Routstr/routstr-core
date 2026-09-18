"""Tests for the admin settings endpoint's handling of secrets (issue #553).

``admin_password`` is no longer a settings field (it lives only as a one-way
hash in the Secret store), so it must never appear in the GET/PATCH payloads.
``nsec`` (in-memory at runtime) and ``upstream_api_key`` (still in the settings
blob) are both redacted on read and ignored on write — they cannot be set
through the general settings endpoint, only through their dedicated paths.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient

from routstr.core.admin import admin_sessions
from routstr.core.db import AsyncSession, TerminalOutcome, TerminalOutcomeEpoch
from routstr.core.settings import SettingsService, settings


@pytest_asyncio.fixture
async def admin_client(
    integration_client: AsyncClient,
) -> AsyncGenerator[AsyncClient, None]:
    """An integration_client pre-authenticated with an admin session token."""
    token = secrets.token_urlsafe(24)
    admin_sessions[token] = int(time.time()) + 3600
    integration_client.headers["Authorization"] = f"Bearer {token}"
    yield integration_client
    admin_sessions.pop(token, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_uses_exact_saved_dates_with_sharing_disabled(
    admin_client: AsyncClient,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "enable_analytics_sharing", False)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=3)
    end = start + timedelta(days=1)
    integration_session.add(
        TerminalOutcomeEpoch(
            epoch=1,
            coverage_start_day=start.date(),
            coverage_end_day=end.date(),
        )
    )
    for name, at, revenue in (
        ("selected-start", start, 1200),
        ("selected-end-excluded", end, 9999),
        ("recent-excluded", today, 7000),
    ):
        integration_session.add(
            TerminalOutcome(
                outcome_id=name,
                terminal_at_ms=int(at.timestamp() * 1000),
                terminal_day=at.date(),
                model_identifier="fixture/model",
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                revenue_msats=revenue,
            )
        )
    await integration_session.commit()
    response = await admin_client.get(
        "/admin/api/usage/dashboard",
        params={
            "hours": 24,
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["analytics_source"] == "terminal_outcomes"
    assert result["summary"]["successful_chat_completions"] == 1
    assert result["summary"]["revenue_msats"] == 1200
    assert result["summary"]["total_tokens"] == 15
    assert result["ledger_coverage"]["complete"] is True
    assert result["ledger_coverage"]["diagnostic_available"] is False
    assert result["error_details"]["errors"] == []
    assert result["model_usage_mix"]["metrics"][0]["total_revenue_msats"] == 1200


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"start_at": "2026-01-01T00:00:00Z"},
        {"start_at": "2026-01-01", "end_at": "2026-01-02"},
        {"start_at": "2026-01-02T00:00:00Z", "end_at": "2026-01-01T00:00:00Z"},
        {"start_at": "2024-01-01T00:00:00Z", "end_at": "2026-01-01T00:00:00Z"},
        {"start_at": "2099-01-01T00:00:00Z", "end_at": "2099-01-02T00:00:00Z"},
    ],
)
async def test_dashboard_rejects_ambiguous_date_ranges(
    admin_client: AsyncClient, params: dict
) -> None:
    response = await admin_client.get("/admin/api/usage/dashboard", params=params)
    assert response.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_settings_omits_admin_password_and_redacts_secrets(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "nsec", "nsec-secret")
    monkeypatch.setattr(settings, "upstream_api_key", "sk-secret")

    resp = await admin_client.get("/admin/api/settings")
    assert resp.status_code == 200

    data = resp.json()
    assert "admin_password" not in data
    assert data["nsec"] == "[REDACTED]"
    assert data["upstream_api_key"] == "[REDACTED]"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_settings_ignores_secret_fields(
    admin_client: AsyncClient,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The PATCH path persists through SettingsService, which needs an
    # initialized current snapshot and a settings row in the shared test DB.
    await SettingsService.initialize(integration_session)
    monkeypatch.setattr(settings, "nsec", "original-nsec")

    resp = await admin_client.patch(
        "/admin/api/settings",
        json={
            "name": "Renamed",
            "nsec": "attacker-nsec",
            "upstream_api_key": "attacker-key",
            "admin_password": "attacker-pw",
        },
    )
    assert resp.status_code == 200

    data = resp.json()
    assert data["name"] == "Renamed"
    assert "admin_password" not in data
    assert data["nsec"] == "[REDACTED]"
    # The live secret was not overwritten through the general settings endpoint.
    assert settings.nsec == "original-nsec"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_existing_sharing_choice_is_saved(
    admin_client: AsyncClient,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await SettingsService.initialize(integration_session)
    monkeypatch.setattr(settings, "enable_analytics_sharing", False)
    resp = await admin_client.patch(
        "/admin/api/settings",
        json={"enable_analytics_sharing": True},
    )
    assert resp.status_code == 200
    assert resp.json()["enable_analytics_sharing"] is True
    saved = await admin_client.get("/admin/api/settings")
    assert saved.json()["enable_analytics_sharing"] is True
    assert "enable_analytics_collection" not in saved.json()
    assert "enable_analytics_v2" not in saved.json()
    stopped = await admin_client.patch(
        "/admin/api/settings",
        json={
            "enable_analytics_sharing": False,
        },
    )
    assert stopped.status_code == 200
    assert stopped.json()["enable_analytics_sharing"] is False
