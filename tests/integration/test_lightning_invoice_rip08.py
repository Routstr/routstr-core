"""RIP-08 lightning invoice endpoint tests.

Verifies both the spec-compliant path (`POST /lightning/invoice` with
`Authorization: Bearer sk-...`) and the legacy path
(`POST /v1/balance/lightning/invoice` with `api_key` in body).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey

RIP08_PATH = "/lightning/invoice"
LEGACY_PATH = "/v1/balance/lightning/invoice"


@pytest_asyncio.fixture
async def patch_invoice_generation() -> Any:
    """Stub out `generate_lightning_invoice` so no mint round-trip is needed."""
    counter = {"n": 0}

    async def fake_generate(amount_sats: int, description: str) -> tuple[str, str]:
        counter["n"] += 1
        return (
            f"lnbc{amount_sats}n1pfakeinvoice{counter['n']}",
            f"payment_hash_{counter['n']}",
        )

    with patch(
        "routstr.lightning.generate_lightning_invoice",
        side_effect=fake_generate,
    ) as m:
        yield m


@pytest_asyncio.fixture
async def seeded_topup_key(integration_session: AsyncSession) -> str:
    """Insert an ApiKey row and return the public `sk-...` form."""
    hashed = "0" * 64
    key = ApiKey(
        hashed_key=hashed,
        balance=0,
        refund_currency="sat",
        refund_mint_url="http://localhost:3338",
    )
    integration_session.add(key)
    await integration_session.commit()
    return f"sk-{hashed}"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [RIP08_PATH, LEGACY_PATH])
async def test_create_invoice_purpose_create(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
    path: str,
) -> None:
    """`purpose=create` works on both paths and requires no auth."""
    resp = await integration_client.post(
        path,
        json={"amount_sats": 1000, "purpose": "create"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["amount_sats"] == 1000
    assert body["bolt11"].startswith("lnbc")
    assert body["invoice_id"]
    assert body["payment_hash"]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [RIP08_PATH, LEGACY_PATH])
async def test_topup_with_authorization_header(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
    seeded_topup_key: str,
    path: str,
) -> None:
    """RIP-08: topup using `Authorization: Bearer sk-...` header (no api_key in body)."""
    resp = await integration_client.post(
        path,
        json={"amount_sats": 500, "purpose": "topup"},
        headers={"Authorization": f"Bearer {seeded_topup_key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["amount_sats"] == 500
    assert body["bolt11"].startswith("lnbc")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [RIP08_PATH, LEGACY_PATH])
async def test_topup_with_legacy_api_key_in_body(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
    seeded_topup_key: str,
    path: str,
) -> None:
    """Legacy: topup with `api_key` in body still accepted on both paths."""
    resp = await integration_client.post(
        path,
        json={
            "amount_sats": 250,
            "purpose": "topup",
            "api_key": seeded_topup_key,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["amount_sats"] == 250


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [RIP08_PATH, LEGACY_PATH])
async def test_topup_missing_auth_returns_401(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
    path: str,
) -> None:
    """Topup without any credential is rejected on both paths."""
    resp = await integration_client.post(
        path,
        json={"amount_sats": 100, "purpose": "topup"},
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [RIP08_PATH, LEGACY_PATH])
async def test_topup_unknown_api_key_returns_404(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
    path: str,
) -> None:
    resp = await integration_client.post(
        path,
        json={"amount_sats": 100, "purpose": "topup"},
        headers={"Authorization": "Bearer sk-deadbeef"},
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [RIP08_PATH, LEGACY_PATH])
async def test_invoice_status_404_for_unknown_id(
    integration_client: AsyncClient,
    path: str,
) -> None:
    base = path.rsplit("/invoice", 1)[0] + "/invoice"
    resp = await integration_client.get(f"{base}/does-not-exist/status")
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_purpose_defaults_to_create(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
) -> None:
    """Per RIP-08, `purpose` may be omitted and defaults to `create`."""
    resp = await integration_client.post(
        RIP08_PATH,
        json={"amount_sats": 100},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["amount_sats"] == 100


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authorization_header_overrides_body_api_key(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
    seeded_topup_key: str,
) -> None:
    """Header api_key wins over body api_key: bogus body must not cause 404."""
    resp = await integration_client.post(
        RIP08_PATH,
        json={
            "amount_sats": 100,
            "purpose": "topup",
            "api_key": "sk-" + "f" * 64,  # bogus body key
        },
        headers={"Authorization": f"Bearer {seeded_topup_key}"},
    )
    assert resp.status_code == 200, resp.text
