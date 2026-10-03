import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.ppqai import PPQAIUpstreamProvider
from routstr.upstream.venice import VeniceUpstreamProvider


def _model() -> Model:
    return Model(
        id="vendor/model",
        name="Model",
        created=0,
        description="Metadata",
        context_length=8192,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="Unknown",
        ),
        pricing=Pricing(
            prompt=1e-6,
            completion=2e-6,
            input_cache_read=1e-8,
            input_cache_write=3e-6,
            request=0.1,
        ),
    )


def _entry(model_id: str, pricing: dict) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "created_at": 0,
        "context_length": 8192,
        "pricing": pricing,
    }


async def _fetch(
    entries: list[dict], metadata: list[dict] | None = None
) -> list[Model]:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://api.ppq.ai/models"),
        text=json.dumps({"data": entries}),
    )
    with (
        patch(
            "routstr.upstream.ppqai._safe_read_request",
            AsyncMock(return_value=response),
        ),
        patch(
            "routstr.upstream.ppqai.async_fetch_openrouter_models",
            AsyncMock(
                return_value=metadata if metadata is not None else [_model().dict()]
            ),
        ),
    ):
        return await PPQAIUpstreamProvider("test-only").fetch_models()


@pytest.mark.asyncio
async def test_ppq_does_not_inherit_other_provider_cache_or_request_rates() -> None:
    (model,) = await _fetch(
        [_entry("vendor/model", {"api": {"input_per_1M": 4, "output_per_1M": 8}})]
    )
    assert model.pricing == Pricing(prompt=4e-6, completion=8e-6)


@pytest.mark.asyncio
async def test_ppq_alias_matches_do_not_share_mutated_prices() -> None:
    models = await _fetch(
        [
            _entry("vendor/model", {"api": {"input_per_1M": 4, "output_per_1M": 8}}),
            _entry("model", {"api": {"input_per_1M": 6, "output_per_1M": 9}}),
        ]
    )
    assert [m.id for m in models] == ["vendor/model", "model"]
    assert [m.pricing.prompt for m in models] == [4e-6, 6e-6]
    assert models[0] is not models[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, []])
async def test_ppq_partial_api_prices_fall_back_per_field_preserving_zero(
    metadata,
) -> None:
    (model,) = await _fetch(
        [
            _entry(
                "vendor/model",
                {
                    "api": {"input_per_1M": 0},
                    "input_per_1M_tokens": 5,
                    "output_per_1M_tokens": 8,
                },
            )
        ],
        metadata,
    )
    assert model.pricing.prompt == 0
    assert model.pricing.completion == 8e-6


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [None, -1, float("inf"), float("nan")])
async def test_ppq_unpriced_or_invalid_native_rate_is_not_replaced_by_openrouter(
    rate,
) -> None:
    assert (
        await _fetch(
            [
                _entry(
                    "vendor/model", {"api": {"input_per_1M": rate, "output_per_1M": 8}}
                )
            ]
        )
        == []
    )


@pytest.mark.parametrize("provider", [PPQAIUpstreamProvider, VeniceUpstreamProvider])
def test_native_catalog_providers_do_not_backfill_generic_cache_rates(provider) -> None:
    model = _model()
    model.pricing = Pricing(prompt=4e-6, completion=8e-6)
    with patch("routstr.upstream.base.backfill_cache_pricing") as backfill:
        adjusted = provider("test-only", provider_fee=1.1)._apply_provider_fee_to_model(
            model
        )
    backfill.assert_not_called()
    assert adjusted.pricing.input_cache_read == 0
    assert adjusted.pricing.prompt == pytest.approx(4.4e-6)
