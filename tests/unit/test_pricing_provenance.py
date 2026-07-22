"""Tests for pricing provenance — ``pricing_source`` on ``Model``.

Provenance makes a price's origin a first-class, queryable fact: ``native`` is
the provider's own (trustworthy) price, ``litellm``/``openrouter`` are curated/
resale estimates, ``manual`` is operator-entered, and ``unresolved`` marks a
model no source could price (imported disabled). These tests drive the tag
through the public provider ``fetch_models`` API and assert it survives the
fee/sats carrier rebuilds that ``refresh`` performs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from routstr.payment.models import (
    Architecture,
    Model,
    Pricing,
    PricingSource,
    TopProvider,
    _update_model_sats_pricing,
    pricing_metadata,
)
from routstr.upstream.generic import GenericUpstreamProvider


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        return _FakeResponse(self._payload)


def _patch_models_endpoint(payload: dict[str, Any]) -> Any:
    return patch(
        "routstr.upstream.generic.httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(payload),
    )


def _model_by_id(models: list[Any], model_id: str) -> Any:
    return next(m for m in models if m.id == model_id)


async def _fetch(payload: dict[str, Any], or_feed: list[dict]) -> list[Model]:
    with _patch_models_endpoint(payload):
        feed = AsyncMock(return_value=or_feed)
        with patch("routstr.payment.models.async_fetch_openrouter_models", feed):
            return await GenericUpstreamProvider(base_url="http://x").fetch_models()


# ---------------------------------------------------------------------------
# generic path tags every truthful source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_native_price_tagged_native() -> None:
    payload = {
        "data": [
            {
                "id": "venice-llama",
                "owned_by": "venice",
                "model_spec": {
                    "pricing": {"input": {"usd": 0.5}, "output": {"usd": 1.5}},
                },
            }
        ]
    }
    models = await _fetch(payload, [])
    model = _model_by_id(models, "venice-llama")
    assert model.pricing_source == PricingSource.NATIVE


@pytest.mark.asyncio
async def test_generic_bare_deepseek_tagged_litellm() -> None:
    payload = {"data": [{"id": "deepseek-chat", "owned_by": "deepseek"}]}
    models = await _fetch(payload, [])
    model = _model_by_id(models, "deepseek-chat")
    assert model.pricing_source == PricingSource.LITELLM


@pytest.mark.asyncio
async def test_generic_openrouter_fallback_tagged_openrouter() -> None:
    payload = {"data": [{"id": "exotic/model-9000", "owned_by": "exotic"}]}
    or_feed = [
        {
            "id": "exotic/model-9000",
            "context_length": 65536,
            "pricing": {"prompt": "0.000005", "completion": "0.000010"},
        }
    ]
    models = await _fetch(payload, or_feed)
    model = _model_by_id(models, "exotic/model-9000")
    assert model.pricing_source == PricingSource.OPENROUTER


@pytest.mark.asyncio
async def test_generic_unresolvable_tagged_unresolved_and_disabled() -> None:
    payload = {"data": [{"id": "nobody-has-priced-this-xyz", "owned_by": "mystery"}]}
    models = await _fetch(payload, [])
    model = _model_by_id(models, "nobody-has-priced-this-xyz")
    assert model.enabled is False
    assert model.pricing_source == PricingSource.UNRESOLVED


# ---------------------------------------------------------------------------
# carrier preservation — the fee/sats rebuilds must not drop provenance
# ---------------------------------------------------------------------------


def _model_with_source(source: PricingSource) -> Model:
    return Model(
        id="m1",
        name="M1",
        created=0,
        description="d",
        context_length=4096,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=1e-06, completion=2e-06),
        top_provider=TopProvider(context_length=4096, max_completion_tokens=2048),
        **pricing_metadata(source),
    )


def test_sats_pricing_rebuild_preserves_provenance() -> None:
    model = _model_with_source(PricingSource.LITELLM)
    rebuilt = _update_model_sats_pricing(model, sats_to_usd=0.0005)
    assert rebuilt.sats_pricing is not None
    assert rebuilt.pricing_source == PricingSource.LITELLM


def test_provider_fee_rebuild_preserves_provenance() -> None:
    model = _model_with_source(PricingSource.NATIVE)
    provider = GenericUpstreamProvider(base_url="http://x")
    rebuilt = provider._apply_provider_fee_to_model(model)
    assert rebuilt.pricing_source == PricingSource.NATIVE


# ---------------------------------------------------------------------------
# OR-fed providers — the feed injection point tags openrouter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_feed_stamps_openrouter_provenance() -> None:
    """Every entry the OpenRouter feed returns carries an ``openrouter`` tag, so
    the ``Model(**model)`` spreads in the OR-fed providers (openai, xai, ...)
    inherit provenance with no per-provider code."""
    from routstr.payment import models as models_mod

    or_payload = {
        "data": [
            {
                "id": "openai/gpt-4o",
                "name": "GPT-4o",
                "pricing": {"prompt": "0.000005", "completion": "0.000015"},
            }
        ]
    }
    embeddings_payload: dict[str, Any] = {"data": []}

    class _Client:
        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def get(self, url: str, timeout: int = 30) -> _FakeResponse:
            payload = or_payload if "embeddings" not in url else embeddings_payload
            return _FakeResponse(payload)

    with patch.object(
        models_mod.httpx, "AsyncClient", lambda *a, **k: _Client()
    ):
        feed = await models_mod.async_fetch_openrouter_models()

    assert feed
    entry = feed[0]
    assert entry["pricing_source"] == PricingSource.OPENROUTER


# ---------------------------------------------------------------------------
# ppqai — per-model native vs unresolved
# ---------------------------------------------------------------------------


class _PPQClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_PPQClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        return _FakeResponse(self._payload)


@pytest.mark.asyncio
async def test_ppqai_standalone_prices_tagged_native_and_unresolved() -> None:
    """A PPQ model with no OpenRouter match is built standalone: its published
    USD price is native; a model PPQ prices at nothing is unresolved."""
    from routstr.upstream.ppqai import PPQAIUpstreamProvider

    payload = {
        "data": [
            {
                "id": "ppq-priced",
                "name": "PPQ Priced",
                "created_at": 0,
                "context_length": 8192,
                "pricing": {"api": {"input_per_1M": 1.0, "output_per_1M": 2.0}},
            },
            {
                "id": "ppq-free",
                "name": "PPQ Free",
                "created_at": 0,
                "context_length": 8192,
                "pricing": {},
            },
        ]
    }

    provider = PPQAIUpstreamProvider(api_key="k")
    with patch(
        "routstr.upstream.ppqai.httpx.AsyncClient",
        lambda *a, **k: _PPQClient(payload),
    ):
        with patch(
            "routstr.upstream.ppqai.async_fetch_openrouter_models",
            AsyncMock(return_value=[]),
        ):
            models = await provider.fetch_models()

    assert _model_by_id(models, "ppq-priced").pricing_source == PricingSource.NATIVE
    assert (
        _model_by_id(models, "ppq-free").pricing_source == PricingSource.UNRESOLVED
    )


@pytest.mark.asyncio
async def test_ppqai_two_ids_matching_one_openrouter_entry_keep_distinct_prices() -> (
    None
):
    """Two PPQ ids that tail-match the same OpenRouter entry must each get their
    own priced model — not two references to one mutated object, which would let
    the last writer's price (and provenance) clobber the other."""
    from routstr.upstream.ppqai import PPQAIUpstreamProvider

    payload = {
        "data": [
            {
                "id": "gpt-4o",
                "name": "GPT-4o (bare)",
                "created_at": 0,
                "context_length": 8192,
                "pricing": {"api": {"input_per_1M": 5.0, "output_per_1M": 15.0}},
            },
            {
                "id": "openai/gpt-4o",
                "name": "GPT-4o (qualified)",
                "created_at": 0,
                "context_length": 8192,
                "pricing": {"api": {"input_per_1M": 3.0, "output_per_1M": 10.0}},
            },
        ]
    }
    # A single OpenRouter entry both PPQ ids resolve to (one by tail-match).
    or_feed = [
        {
            "id": "openai/gpt-4o",
            "name": "GPT-4o",
            "created": 0,
            "description": "d",
            "context_length": 8192,
            "architecture": {
                "modality": "text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "unknown",
                "instruct_type": None,
            },
            "pricing": {"prompt": 0.000001, "completion": 0.000002},
        }
    ]

    provider = PPQAIUpstreamProvider(api_key="k")
    with patch(
        "routstr.upstream.ppqai.httpx.AsyncClient",
        lambda *a, **k: _PPQClient(payload),
    ):
        with patch(
            "routstr.upstream.ppqai.async_fetch_openrouter_models",
            AsyncMock(return_value=or_feed),
        ):
            models = await provider.fetch_models()

    assert len(models) == 2
    # Distinct objects — no shared mutation aliasing the two into one row.
    assert models[0] is not models[1]
    # Each PPQ id kept its own overlaid price; the last writer didn't clobber.
    assert {round(m.pricing.prompt * 1_000_000, 6) for m in models} == {5.0, 3.0}


# ---------------------------------------------------------------------------
# homeless field — supports_function_calling from litellm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_litellm_populates_supports_function_calling() -> None:
    """litellm's ``supports_function_calling`` had no typed home; it now lands on
    ``Architecture`` for models resolved via litellm (deepseek-chat supports it)."""
    payload = {"data": [{"id": "deepseek-chat", "owned_by": "deepseek"}]}
    models = await _fetch(payload, [])
    model = _model_by_id(models, "deepseek-chat")
    assert model.architecture.supports_function_calling is True
