"""Unit tests for ``GenericUpstreamProvider.fetch_models`` price/metadata resolution.

A generic upstream is any OpenAI-compatible API. Most (DeepSeek, OpenAI,
Groq, ...) answer ``/models`` with bare ``{id, object, owned_by}`` entries that
carry *no* pricing. The provider must not fabricate a price for those: it
resolves through native ``model_spec`` (Venice's bespoke schema) → litellm's
bundled cost map → the OpenRouter feed, and only when every source misses does
it import the model **disabled** with a warning rather than invent a number.

These tests drive that behaviour through the public ``fetch_models`` API. The
``/models`` HTTP call is faked at ``httpx.AsyncClient``; the OpenRouter feed is
patched at its source (``routstr.payment.models.async_fetch_openrouter_models``)
so the resolver's lazy import picks up the stub. litellm's real bundled cost map
is used unmocked — the DeepSeek rates it ships are the assertion's ground truth.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from routstr.upstream.generic import GenericUpstreamProvider


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    """Stand-in for ``httpx.AsyncClient`` returning a canned ``/models`` body."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        return _FakeResponse(self._payload)


def _patch_models_endpoint(payload: dict[str, Any]) -> Any:
    """Patch the provider's ``httpx.AsyncClient`` to serve ``payload``."""
    return patch(
        "routstr.upstream.generic.httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(payload),
    )


def _model_by_id(models: list[Any], model_id: str) -> Any:
    return next(m for m in models if m.id == model_id)


# ---------------------------------------------------------------------------
# native model_spec (Venice) — must keep resolving, and capture its metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_model_spec_resolves_and_captures_metadata() -> None:
    """A Venice-shaped ``model_spec`` is authoritative: prices/context come
    straight from it and vision capability becomes an image input modality."""
    payload = {
        "data": [
            {
                "id": "venice-llama",
                "owned_by": "venice",
                "model_spec": {
                    "name": "Venice Llama",
                    "availableContextTokens": 65536,
                    "pricing": {
                        "input": {"usd": 0.5},
                        "output": {"usd": 1.5},
                    },
                    "capabilities": {"supportsVision": True},
                },
            }
        ]
    }

    with _patch_models_endpoint(payload):
        or_feed = AsyncMock(return_value=[])
        with patch(
            "routstr.payment.models.async_fetch_openrouter_models", or_feed
        ):
            models = await GenericUpstreamProvider(base_url="http://x").fetch_models()

    model = _model_by_id(models, "venice-llama")
    assert model.enabled is True
    assert model.pricing.prompt == pytest.approx(0.5 / 1_000_000)
    assert model.pricing.completion == pytest.approx(1.5 / 1_000_000)
    assert model.context_length == 65536
    assert "image" in model.architecture.input_modalities
    # A native price never needs the OpenRouter feed.
    or_feed.assert_not_awaited()


# ---------------------------------------------------------------------------
# litellm rescue — the money-critical case (DeepSeek bare /models)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_deepseek_resolves_via_litellm() -> None:
    """DeepSeek's ``/models`` carries no pricing. The old code fabricated
    ``$0.001`` + ctx 4096; the resolver must instead pull DeepSeek's real
    rates from litellm's bundled cost map (``$0.28``/``$0.42`` per 1M, ctx
    131072) and keep the model enabled."""
    payload = {
        "data": [
            {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
        ]
    }

    with _patch_models_endpoint(payload):
        or_feed = AsyncMock(return_value=[])
        with patch(
            "routstr.payment.models.async_fetch_openrouter_models", or_feed
        ):
            models = await GenericUpstreamProvider(base_url="http://x").fetch_models()

    model = _model_by_id(models, "deepseek-chat")
    assert model.enabled is True
    assert model.pricing.prompt == pytest.approx(2.8e-07)
    assert model.pricing.completion == pytest.approx(4.2e-07)
    assert model.context_length == 131072
    # Richer metadata than the two base prices is captured too.
    assert model.pricing.input_cache_read == pytest.approx(2.8e-08)
    assert model.top_provider is not None
    assert model.top_provider.max_completion_tokens == 8192
    # litellm answered, so the OpenRouter feed is never consulted.
    or_feed.assert_not_awaited()


@pytest.mark.asyncio
async def test_litellm_zero_price_entry_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A litellm entry that lists a model but prices it at 0/0 (free-tier
    moderation/rerank models do this) is not a real price — treating it as one
    would silently serve the model for free. The resolver must reject a both-zero
    litellm hit and fall through, so the model imports disabled, not at $0."""
    payload = {
        "data": [
            {"id": "omni-moderation-latest", "object": "model", "owned_by": "openai"},
        ]
    }

    gen_logger = logging.getLogger("routstr.upstream.generic")
    gen_logger.addHandler(caplog.handler)
    try:
        with _patch_models_endpoint(payload):
            or_feed = AsyncMock(return_value=[])
            with patch(
                "routstr.payment.models.async_fetch_openrouter_models", or_feed
            ):
                models = await GenericUpstreamProvider(
                    base_url="http://x"
                ).fetch_models()
    finally:
        gen_logger.removeHandler(caplog.handler)

    model = _model_by_id(models, "omni-moderation-latest")
    assert model.enabled is False
    assert model.pricing.prompt == 0.0
    assert model.pricing.completion == 0.0
    assert any(
        "omni-moderation-latest" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
    )


@pytest.mark.asyncio
async def test_litellm_output_cap_not_used_as_context() -> None:
    """litellm's ``max_tokens`` is the completion cap, not the context window
    (it tracks ``max_output_tokens`` for ~94% of models). When a model reports
    no ``max_input_tokens``, the resolver must not smuggle the output cap in as
    the context window; it falls back to the id-based estimate instead, while
    ``max_tokens`` still feeds the completion limit."""
    payload = {
        "data": [
            {
                "id": "gemini/gemini-gemma-2-9b-it",
                "object": "model",
                "owned_by": "google",
            },
        ]
    }

    with _patch_models_endpoint(payload):
        or_feed = AsyncMock(return_value=[])
        with patch(
            "routstr.payment.models.async_fetch_openrouter_models", or_feed
        ):
            models = await GenericUpstreamProvider(base_url="http://x").fetch_models()

    model = _model_by_id(models, "gemini/gemini-gemma-2-9b-it")
    assert model.enabled is True
    # litellm gives this model max_input_tokens=None, max_tokens=8192 (an output
    # cap). Context must come from the estimate (4096), never the 8192 cap.
    assert model.context_length == 4096
    # The 8192 output cap still lands where it belongs: the completion limit.
    assert model.top_provider is not None
    assert model.top_provider.max_completion_tokens == 8192


# ---------------------------------------------------------------------------
# OpenRouter fallback — litellm misses, OR carries a full payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_to_litellm_resolves_via_openrouter() -> None:
    """A model litellm has never heard of still resolves if the OpenRouter
    feed lists it, pulling price + context from that entry."""
    payload = {
        "data": [
            {"id": "exotic/model-9000", "object": "model", "owned_by": "exotic"},
        ]
    }
    or_entry = {
        "id": "exotic/model-9000",
        "name": "Exotic 9000",
        "context_length": 65536,
        "architecture": {
            "modality": "text->text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tokenizer": "Other",
            "instruct_type": None,
        },
        "pricing": {"prompt": "0.000005", "completion": "0.000010"},
        "top_provider": {
            "context_length": 65536,
            "max_completion_tokens": 4096,
            "is_moderated": False,
        },
    }

    with _patch_models_endpoint(payload):
        or_feed = AsyncMock(return_value=[or_entry])
        with patch(
            "routstr.payment.models.async_fetch_openrouter_models", or_feed
        ):
            models = await GenericUpstreamProvider(base_url="http://x").fetch_models()

    model = _model_by_id(models, "exotic/model-9000")
    assert model.enabled is True
    assert model.pricing.prompt == pytest.approx(5e-06)
    assert model.pricing.completion == pytest.approx(1e-05)
    assert model.context_length == 65536
    or_feed.assert_awaited()


@pytest.mark.asyncio
async def test_openrouter_bare_tail_collision_picks_highest_price() -> None:
    """When a bare model id matches several OpenRouter entries by tail
    (``model`` ↔ ``a/model``, ``b/model``), the match must be deterministic and
    money-safe: pick the highest-priced candidate regardless of feed order, so
    ordering can never leave the node charging below true cost. (The live feed
    has zero such collisions today; this guards the latent case.)"""
    payload = {
        "data": [
            {"id": "zzz-phantom-model", "object": "model", "owned_by": "mystery"},
        ]
    }
    # Same bare tail, different resellers; the pricier one is listed *second*
    # so a first-wins match would pick the cheaper (undercharging) entry.
    or_feed = AsyncMock(
        return_value=[
            {
                "id": "cheapco/zzz-phantom-model",
                "context_length": 8192,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            },
            {
                "id": "premiumco/zzz-phantom-model",
                "context_length": 8192,
                "pricing": {"prompt": "0.000009", "completion": "0.000010"},
            },
        ]
    )

    with _patch_models_endpoint(payload):
        with patch("routstr.payment.models.async_fetch_openrouter_models", or_feed):
            models = await GenericUpstreamProvider(base_url="http://x").fetch_models()

    model = _model_by_id(models, "zzz-phantom-model")
    assert model.pricing.prompt == pytest.approx(9e-06)
    assert model.pricing.completion == pytest.approx(1e-05)


@pytest.mark.asyncio
async def test_openrouter_feed_fetched_once_per_discovery() -> None:
    """Two models both missing litellm must share a single OpenRouter fetch —
    the feed is not re-downloaded per model."""
    payload = {
        "data": [
            {"id": "exotic/model-a", "object": "model", "owned_by": "exotic"},
            {"id": "exotic/model-b", "object": "model", "owned_by": "exotic"},
        ]
    }
    or_feed = AsyncMock(
        return_value=[
            {
                "id": "exotic/model-a",
                "context_length": 8192,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            },
            {
                "id": "exotic/model-b",
                "context_length": 8192,
                "pricing": {"prompt": "0.000003", "completion": "0.000004"},
            },
        ]
    )

    with _patch_models_endpoint(payload):
        with patch("routstr.payment.models.async_fetch_openrouter_models", or_feed):
            models = await GenericUpstreamProvider(base_url="http://x").fetch_models()

    assert {m.id for m in models} == {"exotic/model-a", "exotic/model-b"}
    assert or_feed.await_count == 1


# ---------------------------------------------------------------------------
# fail closed — no source resolves → disabled + warned, never fabricated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolvable_model_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When native, litellm and OpenRouter all miss, the model is imported
    disabled with a warning naming it — and no price is invented (the old
    ``$0.001`` placeholder is gone)."""
    payload = {
        "data": [
            {
                "id": "nobody-has-priced-this-xyz",
                "object": "model",
                "owned_by": "mystery",
            },
        ]
    }

    # routstr loggers set propagate=False, so caplog's root handler misses
    # them; attach its handler to the provider logger directly.
    gen_logger = logging.getLogger("routstr.upstream.generic")
    gen_logger.addHandler(caplog.handler)
    try:
        with _patch_models_endpoint(payload):
            or_feed = AsyncMock(return_value=[])
            with patch(
                "routstr.payment.models.async_fetch_openrouter_models", or_feed
            ):
                models = await GenericUpstreamProvider(
                    base_url="http://x"
                ).fetch_models()
    finally:
        gen_logger.removeHandler(caplog.handler)

    model = _model_by_id(models, "nobody-has-priced-this-xyz")
    assert model.enabled is False
    assert model.pricing.prompt == 0.0
    assert model.pricing.completion == 0.0
    assert any(
        "nobody-has-priced-this-xyz" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
    )
