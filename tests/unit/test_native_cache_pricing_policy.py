import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.core.db import ModelRow
from routstr.payment.models import Pricing, _row_to_model, list_models
from routstr.upstream.helpers import get_all_models_with_overrides
from routstr.upstream.model_paths import _price_in_sats


def _row(cache_rate: float = 0) -> ModelRow:
    return ModelRow(
        id="vendor/model",
        name="Model",
        created=0,
        description="",
        context_length=8192,
        architecture=json.dumps(
            {
                "modality": "text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "unknown",
            }
        ),
        pricing=json.dumps(
            {"prompt": 4e-6, "completion": 8e-6, "input_cache_read": cache_rate}
        ),
        enabled=True,
        upstream_provider_id=1,
    )


def _session(row: ModelRow, provider_type: str):
    provider = SimpleNamespace(
        id=1, enabled=True, provider_fee=1.1, provider_type=provider_type
    )
    return SimpleNamespace(
        exec=AsyncMock(
            side_effect=[
                SimpleNamespace(all=lambda: [row]),
                SimpleNamespace(all=lambda: [provider]),
            ]
        )
    )


@pytest.mark.parametrize("provider_type", ["ppqai", "venice"])
@pytest.mark.parametrize("cache_rate", [0, 7e-7])
def test_db_conversion_preserves_native_or_explicit_cache_prices(
    provider_type, cache_rate
) -> None:
    row = _row(cache_rate)
    stored = row.pricing
    with (
        patch(
            "routstr.payment.models.backfill_cache_pricing",
            return_value=Pricing(prompt=4e-6, completion=8e-6, input_cache_read=9e-7),
        ) as backfill,
        patch("routstr.payment.models.sats_usd_price", return_value=0.001),
    ):
        model = _row_to_model(row, True, 1.1, provider_type=provider_type)
    backfill.assert_not_called()
    assert model.pricing.input_cache_read == pytest.approx(cache_rate * 1.1)
    assert row.pricing == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["ppqai", "venice"])
async def test_admin_listing_passes_provider_policy_to_database_conversion(
    provider_type,
) -> None:
    with (
        patch("routstr.payment.models.backfill_cache_pricing") as backfill,
        patch("routstr.payment.models.sats_usd_price", return_value=0.001),
    ):
        models = await list_models(_session(_row(), provider_type), upstream_id=1)
    backfill.assert_not_called()
    assert len(models) == 1
    assert models[0].pricing.input_cache_read == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["ppqai", "venice"])
async def test_runtime_overrides_do_not_reintroduce_generic_cache_prices(
    provider_type,
) -> None:
    row = _row()
    session = _session(row, provider_type)

    @asynccontextmanager
    async def create_session():
        yield session

    upstream = SimpleNamespace(
        db_id=1,
        provider_type=provider_type,
        base_url="https://example.invalid",
        get_cached_models=MagicMock(
            return_value=[SimpleNamespace(id=row.id, enabled=True)]
        ),
    )
    with (
        patch("routstr.upstream.helpers.create_session", create_session),
        patch("routstr.payment.models.backfill_cache_pricing") as backfill,
        patch("routstr.payment.models.sats_usd_price", return_value=0.001),
    ):
        models = await get_all_models_with_overrides([upstream])
    backfill.assert_not_called()
    assert len(models) == 1
    assert models[0].pricing.input_cache_read == 0


@pytest.mark.parametrize("provider_type", ["ppqai", "venice"])
def test_path_sats_conversion_respects_native_cache_policy(provider_type) -> None:
    model = {
        "id": "vendor/model",
        "pricing": {"prompt": 4e-6, "completion": 8e-6},
        "context_length": 8192,
    }
    with (
        patch("routstr.payment.models.backfill_cache_pricing") as backfill,
        patch("routstr.payment.price.sats_usd_price", return_value=0.001),
    ):
        _price_in_sats(model, 1.1, provider_type)
    backfill.assert_not_called()
    assert model["pricing"]["input_cache_read"] == 0
    assert model["sats_pricing"]["input_cache_read"] == 0
