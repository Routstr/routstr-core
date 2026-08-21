"""Lightning invoice endpoint compatibility and v2 contract tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey
from routstr.wallet import MintConnectionError

RIP08_PATH = "/lightning/invoice"
LEGACY_PATH = "/v1/balance/lightning/invoice"
V2_PATH = "/v2/lightning/invoice"
COMPATIBILITY_PATHS = [RIP08_PATH, LEGACY_PATH]
ALL_PATHS = [*COMPATIBILITY_PATHS, V2_PATH]


@pytest_asyncio.fixture
async def patch_invoice_generation() -> Any:
    """Stub out `generate_lightning_invoice` so no mint round-trip is needed."""
    counter = {"n": 0}

    async def fake_generate(
        amount_sats: int,
        description: str,
        *,
        allowed_mints: list[str] | None = None,
    ) -> tuple[str, str, str]:
        counter["n"] += 1
        return (
            f"lnbc{amount_sats}n1pfakeinvoice{counter['n']}",
            f"payment_hash_{counter['n']}",
            "http://localhost:3338",
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
@pytest.mark.parametrize("path", ALL_PATHS)
async def test_create_invoice_purpose_create(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
    path: str,
) -> None:
    """`purpose=create` works on every path and requires no auth."""
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
@pytest.mark.parametrize("path", ALL_PATHS)
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
    allowed_mints = patch_invoice_generation.call_args.kwargs["allowed_mints"]
    assert allowed_mints == ["http://localhost:3338"]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("path", ALL_PATHS)
async def test_topup_with_legacy_api_key_in_body(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
    seeded_topup_key: str,
    path: str,
) -> None:
    """The deprecated body `api_key` remains accepted on every path."""
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
@pytest.mark.parametrize("path", COMPATIBILITY_PATHS)
async def test_compatibility_topup_missing_auth_keeps_string_error(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
    path: str,
) -> None:
    resp = await integration_client.post(
        path,
        json={"amount_sats": 100, "purpose": "topup"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == (
        "Authorization bearer api key is required for topup"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_v2_topup_missing_auth_returns_typed_error(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
) -> None:
    resp = await integration_client.post(
        V2_PATH,
        json={"amount_sats": 100, "purpose": "topup"},
    )
    assert resp.status_code == 401
    error = resp.json()["detail"]["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "topup_authorization_required"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,expected_detail",
    [
        (RIP08_PATH, "Invalid API key format"),
        (LEGACY_PATH, "Invalid API key format"),
    ],
)
async def test_compatibility_invalid_api_key_format_keeps_original_message(
    integration_client: AsyncClient,
    path: str,
    expected_detail: str,
) -> None:
    resp = await integration_client.post(
        path,
        json={"amount_sats": 100, "purpose": "topup"},
        headers={"Authorization": "Bearer invalid"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == expected_detail


@pytest.mark.integration
@pytest.mark.asyncio
async def test_v2_invalid_api_key_format_returns_typed_error(
    integration_client: AsyncClient,
) -> None:
    resp = await integration_client.post(
        V2_PATH,
        json={"amount_sats": 100, "purpose": "topup"},
        headers={"Authorization": "Bearer invalid"},
    )
    assert resp.status_code == 400
    error = resp.json()["detail"]["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "topup_invalid_api_key_format"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("path", COMPATIBILITY_PATHS)
async def test_compatibility_unknown_api_key_keeps_string_error(
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
    assert resp.json()["detail"] == "API key not found"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_v2_unknown_api_key_returns_typed_error(
    integration_client: AsyncClient,
    patch_invoice_generation: Any,
) -> None:
    resp = await integration_client.post(
        V2_PATH,
        json={"amount_sats": 100, "purpose": "topup"},
        headers={"Authorization": "Bearer sk-deadbeef"},
    )
    assert resp.status_code == 404
    error = resp.json()["detail"]["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "topup_api_key_not_found"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("path", COMPATIBILITY_PATHS)
async def test_compatibility_status_404_keeps_string_error(
    integration_client: AsyncClient,
    path: str,
) -> None:
    resp = await integration_client.get(f"{path}/does-not-exist/status")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Invoice not found"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_v2_status_404_returns_typed_error(
    integration_client: AsyncClient,
) -> None:
    resp = await integration_client.get(f"{V2_PATH}/does-not-exist/status")
    assert resp.status_code == 404
    error = resp.json()["detail"]["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "invoice_not_found"


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


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status,code",
    [
        (MintConnectionError("all mints failed"), 503, "lightning_mint_unreachable"),
        (RuntimeError("boom"), 500, "invoice_creation_failed"),
    ],
)
async def test_create_invoice_maps_mint_failures(
    integration_client: AsyncClient,
    error: Exception,
    status: int,
    code: str,
) -> None:
    with patch(
        "routstr.lightning.generate_lightning_invoice",
        side_effect=error,
    ):
        resp = await integration_client.post(V2_PATH, json={"amount_sats": 100})

    assert resp.status_code == status
    assert resp.json()["detail"]["error"]["code"] == code


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compatibility_create_failure_keeps_generic_error(
    integration_client: AsyncClient,
) -> None:
    with patch(
        "routstr.lightning.generate_lightning_invoice",
        side_effect=MintConnectionError("all mints failed"),
    ):
        resp = await integration_client.post(RIP08_PATH, json={"amount_sats": 100})

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to create Lightning invoice"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,expected_detail",
    [
        ("/lightning/recover", "Invoice not found"),
        ("/v1/balance/lightning/recover", "Invoice not found"),
    ],
)
async def test_compatibility_recover_404_keeps_string_error(
    integration_client: AsyncClient,
    path: str,
    expected_detail: str,
) -> None:
    resp = await integration_client.post(path, json={"bolt11": "unknown"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == expected_detail


@pytest.mark.integration
@pytest.mark.asyncio
async def test_v2_recover_404_returns_typed_error(
    integration_client: AsyncClient,
) -> None:
    resp = await integration_client.post(
        "/v2/lightning/recover", json={"bolt11": "unknown"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "invoice_not_found"
