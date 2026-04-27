"""Integration tests for provider fee schedule API endpoints."""

from typing import Any, Generator

import pytest
from httpx import AsyncClient

from routstr.core.admin import admin_sessions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ADMIN_TOKEN = "test-admin-token"


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


async def _create_provider(client: AsyncClient, *, fee: float = 1.02) -> int:
    """Create a test provider and return its ID."""
    resp = await client.post(
        "/admin/api/upstream-providers",
        json={
            "provider_type": "custom",
            "base_url": "https://api.example.com/v1",
            "api_key": "test-key",
            "enabled": True,
            "provider_fee": fee,
        },
        headers=_auth_header(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _inject_admin_session() -> Generator[None, None, None]:
    """Inject a valid admin session token for all tests."""
    import time

    admin_sessions[ADMIN_TOKEN] = int(time.time()) + 3600
    yield
    admin_sessions.pop(ADMIN_TOKEN, None)


@pytest.fixture(autouse=True)
def _patch_reinitialize(monkeypatch: Any) -> None:
    async def _noop(*args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr("routstr.core.admin.reinitialize_upstreams", _noop)
    monkeypatch.setattr("routstr.core.admin.refresh_model_maps", _noop)


# ---------------------------------------------------------------------------
# GET fee schedules
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_fee_schedules_empty_for_new_provider(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    resp = await integration_client.get(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        headers=_auth_header(),
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_fee_schedules_404_for_missing_provider(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    resp = await integration_client.get(
        "/admin/api/upstream-providers/99999/fee-schedules",
        headers=_auth_header(),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT fee schedules
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_fee_schedules_success(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    schedules = [
        {"start_time": "08:00", "end_time": "18:00", "provider_fee": 1.05},
        {"start_time": "18:00", "end_time": "08:00", "provider_fee": 1.02},
    ]
    resp = await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={"schedules": schedules},
        headers=_auth_header(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["start_time"] == "08:00"
    assert data[0]["provider_fee"] == 1.05


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_fee_schedules_persisted(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    """Saved schedules are returned by a subsequent GET."""
    provider_id = await _create_provider(integration_client)
    schedules = [{"start_time": "09:00", "end_time": "17:00", "provider_fee": 1.07}]
    await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={"schedules": schedules},
        headers=_auth_header(),
    )
    get_resp = await integration_client.get(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        headers=_auth_header(),
    )
    assert get_resp.status_code == 200
    assert get_resp.json()[0]["provider_fee"] == 1.07


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_fee_schedules_replaces_existing(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    # Set initial schedule
    await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={
            "schedules": [
                {"start_time": "08:00", "end_time": "12:00", "provider_fee": 1.03}
            ]
        },
        headers=_auth_header(),
    )
    # Replace with different schedule
    resp = await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={
            "schedules": [
                {"start_time": "14:00", "end_time": "20:00", "provider_fee": 1.08}
            ]
        },
        headers=_auth_header(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["start_time"] == "14:00"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_fee_schedules_overlap_rejected(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    schedules = [
        {"start_time": "08:00", "end_time": "14:00", "provider_fee": 1.05},
        {"start_time": "12:00", "end_time": "18:00", "provider_fee": 1.03},
    ]
    resp = await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={"schedules": schedules},
        headers=_auth_header(),
    )
    assert resp.status_code == 400
    assert "overlap" in resp.json()["detail"].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_fee_schedules_invalid_time_format_rejected(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    resp = await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={
            "schedules": [
                {"start_time": "8:00", "end_time": "18:00", "provider_fee": 1.05}
            ]
        },
        headers=_auth_header(),
    )
    assert resp.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_fee_schedules_invalid_fee_rejected(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    resp = await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={
            "schedules": [
                {"start_time": "08:00", "end_time": "18:00", "provider_fee": -0.5}
            ]
        },
        headers=_auth_header(),
    )
    assert resp.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_fee_schedules_empty_clears_schedules(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    # Set a schedule
    await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={
            "schedules": [
                {"start_time": "08:00", "end_time": "18:00", "provider_fee": 1.05}
            ]
        },
        headers=_auth_header(),
    )
    # Clear with empty list
    resp = await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={"schedules": []},
        headers=_auth_header(),
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_fee_schedules_404_for_missing_provider(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    resp = await integration_client.put(
        "/admin/api/upstream-providers/99999/fee-schedules",
        json={"schedules": []},
        headers=_auth_header(),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE fee schedules
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_fee_schedules(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    # Add schedules
    await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={
            "schedules": [
                {"start_time": "08:00", "end_time": "18:00", "provider_fee": 1.05}
            ]
        },
        headers=_auth_header(),
    )
    # Delete
    del_resp = await integration_client.delete(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        headers=_auth_header(),
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["ok"] is True

    # Verify schedules are gone
    get_resp = await integration_client.get(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        headers=_auth_header(),
    )
    assert get_resp.json() == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_fee_schedules_404_for_missing_provider(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    resp = await integration_client.delete(
        "/admin/api/upstream-providers/99999/fee-schedules",
        headers=_auth_header(),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Fee schedules appear in provider list and detail
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fee_schedules_in_provider_list(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={
            "schedules": [
                {"start_time": "08:00", "end_time": "18:00", "provider_fee": 1.05}
            ]
        },
        headers=_auth_header(),
    )
    list_resp = await integration_client.get(
        "/admin/api/upstream-providers", headers=_auth_header()
    )
    assert list_resp.status_code == 200
    providers = list_resp.json()
    target = next((p for p in providers if p["id"] == provider_id), None)
    assert target is not None
    assert len(target["provider_fee_schedules"]) == 1
    assert target["provider_fee_schedules"][0]["provider_fee"] == 1.05


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fee_schedules_in_provider_detail(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={
            "schedules": [
                {"start_time": "10:00", "end_time": "22:00", "provider_fee": 1.06}
            ]
        },
        headers=_auth_header(),
    )
    detail_resp = await integration_client.get(
        f"/admin/api/upstream-providers/{provider_id}", headers=_auth_header()
    )
    assert detail_resp.status_code == 200
    data = detail_resp.json()
    assert len(data["provider_fee_schedules"]) == 1
    assert data["provider_fee_schedules"][0]["start_time"] == "10:00"


# ---------------------------------------------------------------------------
# Provider deletion clears fee schedules
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_delete_clears_fee_schedules(
    integration_client: AsyncClient, patched_db_engine: Any
) -> None:
    provider_id = await _create_provider(integration_client)
    await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={
            "schedules": [
                {"start_time": "08:00", "end_time": "18:00", "provider_fee": 1.05}
            ]
        },
        headers=_auth_header(),
    )
    # Delete provider
    del_resp = await integration_client.delete(
        f"/admin/api/upstream-providers/{provider_id}", headers=_auth_header()
    )
    assert del_resp.status_code == 200

    # Provider is gone → schedule endpoint returns 404
    get_resp = await integration_client.get(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        headers=_auth_header(),
    )
    assert get_resp.status_code == 404
