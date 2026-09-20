"""Unit tests for the TypeSafe System One provider integration.

Covers the three integration seams the systemone endpoint touches:

* the proxy path allowlist admits ``systemone`` (POST only) and nothing that
  merely looks like it;
* the X-Cashu settlement gate treats ``systemone`` as a settleable endpoint;
* the provider maps TypeSafe's pricing-less model listing onto priced
  ``Model`` objects with usable rates.
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from routstr.proxy import _forwarding_allowed  # noqa: E402
from routstr.upstream.base import _x_cashu_path_has_settlement_handler  # noqa: E402
from routstr.upstream.typesafe import TypeSafeUpstreamProvider  # noqa: E402


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("v1/systemone", "POST"),
        ("systemone", "POST"),
        ("v1/systemone/", "POST"),
    ],
)
def test_systemone_is_forwarded(path: str, method: str) -> None:
    assert _forwarding_allowed(path, method) is True


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("v1/systemone", "GET"),  # billed endpoint is POST-only
        ("v1/systemone", "DELETE"),
        ("systemonedump", "POST"),  # longer segment must not match
        ("v1/systemone/secret", "POST"),  # trailing id segment widens nothing
        ("v1/systemone/../admin", "POST"),  # traversal spelling is screened
    ],
)
def test_systemone_lookalikes_are_refused(path: str, method: str) -> None:
    assert _forwarding_allowed(path, method) is False


def test_systemone_has_x_cashu_settlement_handler() -> None:
    assert _x_cashu_path_has_settlement_handler("v1/systemone") is True
    assert _x_cashu_path_has_settlement_handler("systemone/") is True


def test_provider_metadata_is_complete() -> None:
    meta = TypeSafeUpstreamProvider.get_provider_metadata()
    assert meta["id"] == "typesafe"
    assert meta["default_base_url"] == "https://api.typesafe.ai/v1"
    assert meta["fixed_base_url"] is True


def test_provider_transform_model_name() -> None:
    provider = TypeSafeUpstreamProvider(api_key="test")
    assert provider.transform_model_name("typesafe/jev-latest") == "jev-latest"
    assert provider.transform_model_name("jev-latest") == "jev-latest"


def _mock_models_response(payload: dict) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = payload
    return mock_response


def _patch_client(mock_response: MagicMock) -> Any:
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)
    return patch(
        "routstr.upstream.typesafe.httpx.AsyncClient",
        return_value=mock_client,
    )


@pytest.mark.asyncio
async def test_fetch_models_prices_the_listing() -> None:
    provider = TypeSafeUpstreamProvider(api_key="test")
    payload = {
        "models": [
            {
                "name": "jev-latest",
                "description": "The most recent stable release.",
                "release_date": "2026-09-17",
            },
            {
                "name": "jev-preview",
                "description": "The most recent release.",
                "release_date": "2026-09-17",
            },
        ]
    }

    with _patch_client(_mock_models_response(payload)):
        models = await provider.fetch_models()

    assert [m.id for m in models] == ["jev-latest", "jev-preview"]
    for model in models:
        # Input priced, output free; rates usable (zero is a real price).
        assert model.pricing.prompt == pytest.approx(0.042 / 1_000_000)
        assert model.pricing.completion == 0.0
        assert model.context_length == 64_000
        assert model.architecture.input_modalities == ["text"]
        assert model.architecture.output_modalities == ["decisions"]


@pytest.mark.asyncio
async def test_fetch_models_handles_error() -> None:
    provider = TypeSafeUpstreamProvider(api_key="test")
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock(
        side_effect=RuntimeError("upstream down")
    )

    with _patch_client(mock_response):
        models = await provider.fetch_models()

    assert models == []


@pytest.mark.asyncio
async def test_fetch_models_defaults_unknown_model_rates() -> None:
    """A newly listed model still gets a usable default rate."""
    provider = TypeSafeUpstreamProvider(api_key="test")
    payload = {
        "models": [
            {"name": "jev-2.0", "description": "Future model", "release_date": None}
        ]
    }

    with _patch_client(_mock_models_response(payload)):
        models = await provider.fetch_models()

    assert len(models) == 1
    assert models[0].pricing.prompt == pytest.approx(0.042 / 1_000_000)
