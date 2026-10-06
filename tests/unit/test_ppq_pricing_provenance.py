import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from routstr.algorithm import create_model_mappings
from routstr.core.db import ModelRow
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
            instruct_type=None,
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


@pytest.mark.parametrize("matched", [True, False])
@pytest.mark.asyncio
async def test_ppq_uses_its_own_published_cache_rates(matched: bool) -> None:
    # Pricing block as GET https://api.ppq.ai/models returned it on 2026-10-06.
    (model,) = await _fetch(
        [
            _entry(
                "vendor/model",
                {
                    "type": "per_token",
                    "currency": "USD",
                    "input_per_1M_tokens": 1.055,
                    "output_per_1M_tokens": 5.275,
                    "cache_read_per_1M_tokens": 0.1055,
                    "cache_write_per_1M_tokens": 1.31875,
                },
            )
        ],
        metadata=None if matched else [],
    )
    assert model.pricing.dict() == pytest.approx(
        Pricing(
            prompt=1.055e-6,
            completion=5.275e-6,
            input_cache_read=0.1055e-6,
            input_cache_write=1.31875e-6,
        ).dict()
    )


@pytest.mark.parametrize("cache_read", [None, -1.0, float("nan")])
@pytest.mark.asyncio
async def test_ppq_missing_or_invalid_cache_rate_stays_zero(
    cache_read: float | None,
) -> None:
    (model,) = await _fetch(
        [
            _entry(
                "vendor/model",
                {
                    "input_per_1M_tokens": 1.0,
                    "output_per_1M_tokens": 2.0,
                    "cache_read_per_1M_tokens": cache_read,
                    "cache_write_per_1M_tokens": None,
                },
            )
        ]
    )
    assert model.pricing == Pricing(prompt=1e-6, completion=2e-6)


@pytest.mark.asyncio
async def test_ppq_alias_matches_emit_first_stable_id_once() -> None:
    models = await _fetch(
        [
            _entry("vendor/model", {"api": {"input_per_1M": 4, "output_per_1M": 8}}),
            _entry("model", {"api": {"input_per_1M": 6, "output_per_1M": 9}}),
        ]
    )
    assert [m.id for m in models] == ["vendor/model"]
    assert models[0].pricing.prompt == 4e-6


@pytest.mark.asyncio
async def test_ppq_suffix_match_keeps_disabled_model_unroutable() -> None:
    metadata = _model().copy(
        update={"id": "openai/gpt-4o", "canonical_slug": "openai/gpt-4o"}
    )
    discovered = await _fetch(
        [_entry("gpt-4o", {"api": {"input_per_1M": 4, "output_per_1M": 8}})],
        [metadata.dict()],
    )
    provider = PPQAIUpstreamProvider("test-only")
    provider.db_id = 7
    with patch.object(provider, "get_cached_models", return_value=discovered):
        _, provider_map, unique_models = create_model_mappings(
            [provider], {}, {("openai/gpt-4o", 7)}
        )
    assert provider_map == {}
    assert unique_models == {}
    assert discovered[0].id == metadata.id
    assert provider.transform_model_name(discovered[0].id) == metadata.id


@pytest.mark.asyncio
async def test_ppq_suffix_match_keeps_override_as_only_candidate() -> None:
    metadata = _model().copy(
        update={"id": "openai/gpt-4o", "canonical_slug": "openai/gpt-4o"}
    )
    discovered = await _fetch(
        [_entry("gpt-4o", {"api": {"input_per_1M": 4, "output_per_1M": 8}})],
        [metadata.dict()],
    )
    provider = PPQAIUpstreamProvider("test-only")
    provider.db_id = 7
    override = ModelRow(
        id=metadata.id,
        name=metadata.name,
        created=0,
        description="",
        context_length=8192,
        architecture=metadata.architecture.json(),
        pricing=Pricing(prompt=9e-6, completion=18e-6).json(),
        enabled=True,
        upstream_provider_id=7,
        forwarded_model_id="operator-alias",
    )
    with (
        patch.object(provider, "get_cached_models", return_value=discovered),
        patch("routstr.payment.models.sats_usd_price", return_value=0.001),
    ):
        _, provider_map, _ = create_model_mappings(
            [provider], {(metadata.id, 7): (override, 1.0)}, set()
        )
    assert metadata.id in provider_map
    assert "operator-alias" in provider_map
    for candidates in provider_map.values():
        assert len(candidates) == 1
        candidate, serving = candidates[0]
        assert serving is provider
        assert candidate.id == metadata.id
        assert candidate.forwarded_model_id == "operator-alias"
        assert candidate.pricing.prompt == 9e-6


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, []])
async def test_ppq_partial_api_prices_fall_back_per_field_preserving_zero(
    metadata: list[dict] | None,
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
    rate: float | None,
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
def test_native_catalog_providers_do_not_backfill_generic_cache_rates(
    provider: type[PPQAIUpstreamProvider | VeniceUpstreamProvider],
) -> None:
    model = _model()
    model.pricing = Pricing(prompt=4e-6, completion=8e-6)
    with patch("routstr.upstream.base.backfill_cache_pricing") as backfill:
        adjusted = provider("test-only", provider_fee=1.1)._apply_provider_fee_to_model(
            model
        )
    backfill.assert_not_called()
    assert adjusted.pricing.input_cache_read == 0
    assert adjusted.pricing.prompt == pytest.approx(4.4e-6)
