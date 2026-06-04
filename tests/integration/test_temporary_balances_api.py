from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import col, update
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import admin_sessions
from routstr.core.db import ApiKey


def _admin_headers() -> dict[str, str]:
    token = "test-admin-token"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    return {"Authorization": f"Bearer {token}"}


async def _add_key(
    session: AsyncSession,
    hashed_key: str,
    *,
    balance: int = 0,
    total_spent: int = 0,
    total_requests: int = 0,
    created_at: int | None = None,
    parent_key_hash: str | None = None,
    refund_address: str | None = None,
) -> ApiKey:
    key = ApiKey(
        hashed_key=hashed_key,
        balance=balance,
        total_spent=total_spent,
        total_requests=total_requests,
        parent_key_hash=parent_key_hash,
        refund_address=refund_address,
    )
    key.created_at = created_at
    session.add(key)
    await session.commit()

    # The model's default_factory is translated into a SQLAlchemy column
    # default that fires on INSERT whenever the value is None, so a true NULL
    # (a legacy row created before the column existed) can only be produced by
    # an explicit UPDATE after insert.
    if created_at is None:
        await session.exec(
            update(ApiKey)  # type: ignore[call-overload]
            .where(col(ApiKey.hashed_key) == hashed_key)
            .values(created_at=None)
        )
        await session.commit()
    return key


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporary_balances_envelope_and_created_at(
    integration_client: httpx.AsyncClient,
    integration_session: AsyncSession,
) -> None:
    await _add_key(integration_session, "key_a", balance=1000, created_at=1000)

    response = await integration_client.get(
        "/admin/api/temporary-balances", headers=_admin_headers()
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"balances", "total", "totals"}
    assert body["total"] == 1
    assert body["balances"][0]["hashed_key"] == "key_a"
    assert body["balances"][0]["created_at"] == 1000


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporary_balances_sorted_latest_first_nulls_last(
    integration_client: httpx.AsyncClient,
    integration_session: AsyncSession,
) -> None:
    await _add_key(integration_session, "older", created_at=1000)
    await _add_key(integration_session, "newer", created_at=2000)
    await _add_key(integration_session, "legacy", created_at=None)

    response = await integration_client.get(
        "/admin/api/temporary-balances", headers=_admin_headers()
    )

    assert response.status_code == 200
    order = [b["hashed_key"] for b in response.json()["balances"]]
    # Newest created first, NULL created_at (legacy) sorts last.
    assert order == ["newer", "older", "legacy"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporary_balances_pagination(
    integration_client: httpx.AsyncClient,
    integration_session: AsyncSession,
) -> None:
    for i in range(5):
        await _add_key(integration_session, f"key_{i}", created_at=1000 + i)

    headers = _admin_headers()
    page1 = (
        await integration_client.get(
            "/admin/api/temporary-balances?limit=2&offset=0", headers=headers
        )
    ).json()
    page2 = (
        await integration_client.get(
            "/admin/api/temporary-balances?limit=2&offset=2", headers=headers
        )
    ).json()

    assert page1["total"] == 5
    assert page2["total"] == 5
    assert [b["hashed_key"] for b in page1["balances"]] == ["key_4", "key_3"]
    assert [b["hashed_key"] for b in page2["balances"]] == ["key_2", "key_1"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporary_balances_totals_exclude_child_balance(
    integration_client: httpx.AsyncClient,
    integration_session: AsyncSession,
) -> None:
    await _add_key(
        integration_session,
        "parent",
        balance=5000,
        total_spent=100,
        total_requests=3,
        created_at=1000,
    )
    # Child draws from parent's balance, so its balance must NOT be summed,
    # but its spent/requests still count.
    await _add_key(
        integration_session,
        "child",
        balance=0,
        total_spent=200,
        total_requests=7,
        created_at=1001,
        parent_key_hash="parent",
    )

    response = await integration_client.get(
        "/admin/api/temporary-balances", headers=_admin_headers()
    )

    totals = response.json()["totals"]
    assert totals["total_balance"] == 5000
    assert totals["total_spent"] == 300
    assert totals["total_requests"] == 10


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporary_balances_search_filters_total_and_totals(
    integration_client: httpx.AsyncClient,
    integration_session: AsyncSession,
) -> None:
    await _add_key(
        integration_session,
        "alpha",
        balance=1000,
        created_at=1000,
        refund_address="alice@ln.tld",
    )
    await _add_key(
        integration_session,
        "beta",
        balance=2000,
        created_at=1001,
        refund_address="bob@ln.tld",
    )

    headers = _admin_headers()

    # Match by hashed_key.
    by_key = (
        await integration_client.get(
            "/admin/api/temporary-balances?search=alpha", headers=headers
        )
    ).json()
    assert by_key["total"] == 1
    assert by_key["balances"][0]["hashed_key"] == "alpha"
    # totals reflect only the filtered set.
    assert by_key["totals"]["total_balance"] == 1000

    # Match by refund_address.
    by_addr = (
        await integration_client.get(
            "/admin/api/temporary-balances?search=bob@ln.tld", headers=headers
        )
    ).json()
    assert by_addr["total"] == 1
    assert by_addr["balances"][0]["hashed_key"] == "beta"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporary_balances_requires_admin(
    integration_client: httpx.AsyncClient,
) -> None:
    response = await integration_client.get("/admin/api/temporary-balances")
    assert response.status_code in (401, 403)
