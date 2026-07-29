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
    has_chargeable_price,
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
# has_chargeable_price — the money-safety invariant over every billable field
# ---------------------------------------------------------------------------


def test_has_chargeable_price_true_when_any_billable_rate_positive() -> None:
    assert has_chargeable_price(Pricing(prompt=1e-06, completion=0.0)) is True
    assert has_chargeable_price(Pricing(prompt=0.0, completion=1e-06)) is True
    # A per-request charge alone makes a model chargeable even at zero per-token
    # rates — the reason the guard can't look at prompt+completion only.
    assert (
        has_chargeable_price(Pricing(prompt=0.0, completion=0.0, request=0.5)) is True
    )


def test_has_chargeable_price_false_when_all_billable_rates_zero() -> None:
    assert has_chargeable_price(Pricing(prompt=0.0, completion=0.0)) is False


def test_has_chargeable_price_false_for_non_finite_rates() -> None:
    """``inf > 0`` is True, so an infinite rate would read as a real price and be
    served, routed, and billed as ``inf``. Only a finite positive rate can bill a
    sane amount, so non-finite rates are not chargeable — including alongside a
    valid one, since that field can still be the one a request bills on."""
    assert has_chargeable_price(Pricing(prompt=float("inf"), completion=0.0)) is False
    assert has_chargeable_price(Pricing(prompt=float("nan"), completion=0.0)) is False
    assert has_chargeable_price(Pricing(prompt=1e-06, completion=float("inf"))) is False


def test_has_chargeable_price_false_when_one_rate_is_negative() -> None:
    """One positive field must not hide a malformed negative billable field.

    Upstream catalogs and foreign/direct database writers bypass the admin
    validator. If this shared predicate accepts the row, listing and routing use
    it as their money-safety backstop and a request can still bill on the
    negative component.
    """
    pricing = Pricing(prompt=-1.0, completion=1.0)

    assert has_chargeable_price(pricing) is False


@pytest.mark.asyncio
async def test_openrouter_rung_rejects_non_finite_feed_price() -> None:
    """``json.loads`` accepts bare ``NaN``/``Infinity`` and overflows ``1e999`` to
    ``inf``, and ``float("Infinity")`` parses from a feed string — so a non-finite
    rate can reach the resolver from any upstream catalog. It must not be reported
    as a resolved ``openrouter`` price; the model imports unresolved and disabled.
    """
    payload = {"data": [{"id": "junk/model-8000", "owned_by": "junk"}]}
    or_feed = [
        {
            "id": "junk/model-8000",
            "context_length": 8192,
            "pricing": {"prompt": "Infinity", "completion": "0.000008"},
        }
    ]
    models = await _fetch(payload, or_feed)
    model = _model_by_id(models, "junk/model-8000")
    assert model.pricing_source == PricingSource.UNRESOLVED
    assert model.enabled is False


@pytest.mark.asyncio
async def test_litellm_rung_rejects_non_finite_entry_price() -> None:
    """The litellm rung guards negatives and both-zero but not non-finite values,
    which would otherwise be stamped ``litellm`` and served at an insane rate."""
    entry = {
        "input_cost_per_token": float("inf"),
        "output_cost_per_token": 8e-06,
        "max_input_tokens": 8192,
    }
    payload = {"data": [{"id": "litellm-junk-model", "owned_by": "junk"}]}
    with patch("routstr.payment.models.litellm_cost_entry", lambda model_id: entry):
        models = await _fetch(payload, [])
    model = _model_by_id(models, "litellm-junk-model")
    assert model.pricing_source == PricingSource.UNRESOLVED
    assert model.enabled is False


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

    with patch.object(models_mod.httpx, "AsyncClient", lambda *a, **k: _Client()):
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
    assert _model_by_id(models, "ppq-free").pricing_source == PricingSource.UNRESOLVED


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


@pytest.mark.asyncio
async def test_ppqai_matched_partial_price_keeps_openrouter_provenance() -> None:
    """When PPQ prices only one side of a model that matched OpenRouter, the
    other side is still OpenRouter-derived — so the whole-Pricing tag must stay
    ``openrouter``, not claim the model is fully ``native``."""
    from routstr.upstream.ppqai import PPQAIUpstreamProvider

    payload = {
        "data": [
            {
                "id": "gpt-4o",
                "name": "GPT-4o",
                "created_at": 0,
                "context_length": 8192,
                "pricing": {"api": {"input_per_1M": 5.0}},  # no output_per_1M
            }
        ]
    }
    or_feed = [
        {
            "id": "gpt-4o",
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
            "pricing_source": "openrouter",
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

    model = _model_by_id(models, "gpt-4o")
    assert model.pricing_source == PricingSource.OPENROUTER


@pytest.mark.asyncio
async def test_ppqai_native_price_does_not_retain_openrouter_auxiliary_rates() -> None:
    """Whole-``Pricing`` provenance cannot say ``native`` while billable fields
    still come from OpenRouter.

    PPQ publishes only input/output token prices. Once both PPQ rates replace the
    matched token rates and the model is relabelled ``native``, unrelated
    OpenRouter request/image/cache charges must not survive on that price.
    Otherwise Routstr can bill fees PPQ never published while the audit tag says
    every rate is provider-native.
    """
    from routstr.upstream.ppqai import PPQAIUpstreamProvider

    payload = {
        "data": [
            {
                "id": "gpt-4o",
                "name": "GPT-4o",
                "created_at": 0,
                "context_length": 8192,
                "pricing": {
                    "api": {"input_per_1M": 5.0, "output_per_1M": 15.0}
                },
            }
        ]
    }
    or_feed = [
        {
            "id": "gpt-4o",
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
            "pricing": {
                "prompt": 0.000001,
                "completion": 0.000002,
                "request": 0.25,
                "image": 0.5,
                "web_search": 0.75,
                "internal_reasoning": 0.0000003,
                "input_cache_read": 0.0000001,
                "input_cache_write": 0.0000002,
            },
            "pricing_source": "openrouter",
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

    model = _model_by_id(models, "gpt-4o")
    assert model.pricing_source == PricingSource.NATIVE
    assert model.pricing.prompt == pytest.approx(5.0 / 1_000_000)
    assert model.pricing.completion == pytest.approx(15.0 / 1_000_000)
    assert model.pricing.request == 0.0
    assert model.pricing.image == 0.0
    assert model.pricing.web_search == 0.0
    assert model.pricing.internal_reasoning == 0.0
    assert model.pricing.input_cache_read == 0.0
    assert model.pricing.input_cache_write == 0.0


@pytest.mark.asyncio
async def test_ppqai_matched_zero_side_does_not_zero_openrouter_price() -> None:
    """A PPQ price of 0 on one side means PPQ did not really price that side, not
    that the tokens are free: overwriting the matched OpenRouter rate with $0
    would bill those tokens at nothing. The OpenRouter price must stand (and the
    tag stay ``openrouter``); only the truly-priced side is overlaid."""
    from routstr.upstream.ppqai import PPQAIUpstreamProvider

    payload = {
        "data": [
            {
                "id": "gpt-4o",
                "name": "GPT-4o",
                "created_at": 0,
                "context_length": 8192,
                # PPQ prices output but reports input as 0 (absent-as-zero).
                "pricing": {"api": {"input_per_1M": 0, "output_per_1M": 15.0}},
            }
        ]
    }
    or_feed = [
        {
            "id": "gpt-4o",
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
            "pricing_source": "openrouter",
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

    model = _model_by_id(models, "gpt-4o")
    # OpenRouter's input price survives PPQ's zero; the real output side overlays.
    assert model.pricing.prompt == pytest.approx(0.000001)
    assert model.pricing.completion == pytest.approx(15.0 / 1_000_000)
    assert model.pricing_source == PricingSource.OPENROUTER


@pytest.mark.asyncio
async def test_ppqai_unmatched_partial_price_is_unresolved_and_disabled() -> None:
    """An unmatched PPQ model priced on only one side has no trustworthy full
    price: billing the zero side at nothing, so it imports ``unresolved`` and
    disabled rather than a confidently-``native`` half price."""
    from routstr.upstream.ppqai import PPQAIUpstreamProvider

    payload = {
        "data": [
            {
                "id": "ppq-input-only",
                "name": "PPQ Input Only",
                "created_at": 0,
                "context_length": 8192,
                "pricing": {"api": {"input_per_1M": 1.0}},  # no output_per_1M
            }
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

    model = _model_by_id(models, "ppq-input-only")
    assert model.pricing_source == PricingSource.UNRESOLVED
    assert model.enabled is False


@pytest.mark.asyncio
async def test_ppqai_matched_negative_price_does_not_overwrite_openrouter() -> None:
    """A negative PPQ rate is malformed upstream data, not a real price: it is
    truthy, so overlaying it would replace the matched OpenRouter rate with a
    negative value and then mislabel the model ``native``. Only a finite,
    positive PPQ price may override OpenRouter's."""
    from routstr.upstream.ppqai import PPQAIUpstreamProvider

    payload = {
        "data": [
            {
                "id": "gpt-4o",
                "name": "GPT-4o",
                "created_at": 0,
                "context_length": 8192,
                "pricing": {"api": {"input_per_1M": -5.0, "output_per_1M": -15.0}},
            }
        ]
    }
    or_feed = [
        {
            "id": "gpt-4o",
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
            "pricing_source": "openrouter",
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

    model = _model_by_id(models, "gpt-4o")
    assert model.pricing.prompt == pytest.approx(0.000001)
    assert model.pricing.completion == pytest.approx(0.000002)
    assert model.pricing_source == PricingSource.OPENROUTER


@pytest.mark.asyncio
async def test_ppqai_standalone_negative_price_is_unresolved_and_disabled() -> None:
    """An unmatched PPQ model priced with negative rates has no trustworthy
    price: it must import ``unresolved`` and disabled, never ``native`` and
    enabled with a negative rate that would credit the user per token."""
    from routstr.upstream.ppqai import PPQAIUpstreamProvider

    payload = {
        "data": [
            {
                "id": "ppq-negative",
                "name": "PPQ Negative",
                "created_at": 0,
                "context_length": 8192,
                "pricing": {"api": {"input_per_1M": -1.0, "output_per_1M": -2.0}},
            }
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

    model = _model_by_id(models, "ppq-negative")
    assert model.pricing_source == PricingSource.UNRESOLVED
    assert model.enabled is False
    assert model.pricing.prompt >= 0
    assert model.pricing.completion >= 0


# ---------------------------------------------------------------------------
# ollama — no native price; resolve through the shared fallback or unresolved
# ---------------------------------------------------------------------------


async def _fetch_ollama(tags: dict[str, Any], or_feed: list[dict]) -> list[Model]:
    from routstr.upstream.ollama import OllamaUpstreamProvider

    provider = OllamaUpstreamProvider(base_url="http://ollama")
    with patch(
        "routstr.upstream.ollama.httpx.AsyncClient",
        lambda *a, **k: _FakeAsyncClient(tags),
    ):
        with patch(
            "routstr.payment.models.async_fetch_openrouter_models",
            AsyncMock(return_value=or_feed),
        ):
            return await provider.fetch_models()


@pytest.mark.asyncio
async def test_ollama_resolvable_model_uses_source_price_not_native() -> None:
    """Ollama's ``/api/tags`` carries no pricing, so a model must be priced by
    the shared resolver and wear that source — never a hard-coded ``native``
    rate that misreports where the price came from."""
    tags = {"models": [{"name": "exotic-ollama-zzz", "details": {}}]}
    or_feed = [
        {
            "id": "exotic-ollama-zzz",
            "context_length": 8192,
            "pricing": {"prompt": "0.000004", "completion": "0.000008"},
        }
    ]
    models = await _fetch_ollama(tags, or_feed)
    model = _model_by_id(models, "exotic-ollama-zzz")
    assert model.pricing_source == PricingSource.OPENROUTER
    assert model.pricing.prompt == 0.000004


@pytest.mark.asyncio
async def test_ollama_unresolvable_model_is_unresolved_and_disabled() -> None:
    """A model no source can price must import disabled as ``unresolved``, not
    enabled at a fabricated flat rate."""
    tags = {"models": [{"name": "nobody-prices-this-ollama-qqq", "details": {}}]}
    models = await _fetch_ollama(tags, [])
    model = _model_by_id(models, "nobody-prices-this-ollama-qqq")
    assert model.pricing_source == PricingSource.UNRESOLVED
    assert model.enabled is False


# ---------------------------------------------------------------------------
# tinfoil — its own price is native; a missing one must not import free
# ---------------------------------------------------------------------------


async def _fetch_tinfoil(payload: dict[str, Any], or_feed: list[dict]) -> list[Model]:
    from routstr.upstream.tinfoil import TinfoilUpstreamProvider

    provider = TinfoilUpstreamProvider(api_key="tf-test-key")
    with patch(
        "routstr.upstream.tinfoil.httpx.AsyncClient",
        lambda *a, **k: _FakeAsyncClient(payload),
    ):
        with patch(
            "routstr.payment.models.async_fetch_openrouter_models",
            AsyncMock(return_value=or_feed),
        ):
            return await provider.fetch_models()


@pytest.mark.asyncio
async def test_tinfoil_native_price_is_tagged_native() -> None:
    """Tinfoil publishes its own per-1M rates, so a priced model is ``native``.

    Untagged, a re-post of the fetched model through the admin upsert is read as
    a hand-added price and laundered into ``manual`` — which then exempts it from
    the free-row force-disable guard.
    """
    payload = {
        "data": [
            {
                "id": "llama-tinfoil-3",
                "context_window": 16384,
                "pricing": {
                    "inputTokenPricePer1M": 0.5,
                    "outputTokenPricePer1M": 1.5,
                },
            }
        ]
    }
    models = await _fetch_tinfoil(payload, [])
    model = _model_by_id(models, "llama-tinfoil-3")
    assert model.pricing_source == PricingSource.NATIVE
    assert model.pricing.prompt == 0.5 / 1_000_000
    assert model.enabled is True


@pytest.mark.asyncio
async def test_tinfoil_missing_price_resolves_through_shared_chain() -> None:
    """A Tinfoil model with no pricing must be priced by the shared resolver and
    wear that source, not import at Tinfoil's ``0.0`` schema defaults."""
    payload = {"data": [{"id": "exotic-tinfoil-zzz", "context_window": 8192}]}
    or_feed = [
        {
            "id": "exotic-tinfoil-zzz",
            "context_length": 8192,
            "pricing": {"prompt": "0.000004", "completion": "0.000008"},
        }
    ]
    models = await _fetch_tinfoil(payload, or_feed)
    model = _model_by_id(models, "exotic-tinfoil-zzz")
    assert model.pricing_source == PricingSource.OPENROUTER
    assert model.pricing.prompt == 0.000004
    assert model.enabled is True


@pytest.mark.asyncio
async def test_tinfoil_unpriceable_model_is_unresolved_and_disabled() -> None:
    """Tinfoil's pricing fields default to ``0.0``, so a model it ships without
    pricing that no source can price would otherwise import enabled at $0 and
    serve every request free. It must import disabled as ``unresolved``."""
    payload = {
        "data": [{"id": "nobody-prices-this-tinfoil-qqq", "context_window": 4096}]
    }
    models = await _fetch_tinfoil(payload, [])
    model = _model_by_id(models, "nobody-prices-this-tinfoil-qqq")
    assert model.pricing_source == PricingSource.UNRESOLVED
    assert model.enabled is False


@pytest.mark.asyncio
async def test_tinfoil_non_finite_price_is_not_treated_as_native() -> None:
    """``json.loads`` accepts bare ``Infinity``/``NaN`` (and ``1e999`` overflows
    to ``inf``), so a junk Tinfoil rate must fall through to the shared chain
    rather than be published as a trusted native price."""
    payload = {
        "data": [
            {
                "id": "deepseek-chat",
                "context_window": 8192,
                "pricing": {
                    "inputTokenPricePer1M": float("inf"),
                    "outputTokenPricePer1M": float("nan"),
                },
            }
        ]
    }
    models = await _fetch_tinfoil(payload, [])
    model = _model_by_id(models, "deepseek-chat")
    assert model.pricing_source == PricingSource.LITELLM
    assert has_chargeable_price(model.pricing) is True


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
