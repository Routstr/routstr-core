"""Unit tests for ``VeniceUpstreamProvider.fetch_models``.

Venice answers ``/models`` with only its text catalog unless ``type`` is
passed, which is why the same account configured as a generic upstream sees a
different catalog. These tests pin that query parameter, the per-family pricing
shapes, the tier lookup they feed, and the families dropped as unpriceable.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from routstr.payment.image_pricing import per_image_sats
from routstr.upstream.image_generation import parse_json_body
from routstr.upstream.venice import VeniceUpstreamProvider


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload: dict[str, Any], calls: list[dict[str, Any]]) -> None:
        self._payload = payload
        self._calls = calls

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self._calls.append({"url": url, "params": params, "headers": headers})
        return _FakeResponse(self._payload)


CATALOG: dict[str, Any] = {
    "object": "list",
    "data": [
        {
            "id": "venice-uncensored-1-2",
            "type": "text",
            "created": 1727966436,
            "model_spec": {
                "name": "Venice Uncensored 1.2",
                "availableContextTokens": 128000,
                "maxCompletionTokens": 8192,
                "capabilities": {"supportsVision": True},
                "pricing": {
                    "input": {"usd": 0.2, "diem": 0.2},
                    "output": {"usd": 0.9, "diem": 0.9},
                    "cache_input": {"usd": 0.02, "diem": 0.02},
                    "cache_write": {"usd": 0.25, "diem": 0.25},
                },
            },
        },
        {
            "id": "text-embedding-bge-m3",
            "type": "embedding",
            "created": 1727966436,
            "model_spec": {
                "name": "BGE m3",
                "availableContextTokens": 8192,
                "pricing": {"input": {"usd": 0.01, "diem": 0.01}},
            },
        },
        {
            "id": "unpriced-text",
            "type": "text",
            "created": 1727966436,
            "model_spec": {"name": "Unpriced", "pricing": {}},
        },
        {
            "id": "offline-model",
            "type": "text",
            "created": 1727966436,
            "model_spec": {
                "name": "Offline",
                "offline": True,
                "pricing": {"input": {"usd": 0.2, "diem": 0.2}},
            },
        },
        {
            "id": "venice-sd35",
            "type": "image",
            "created": 1727966436,
            "model_spec": {
                "name": "Venice SD35",
                "pricing": {
                    "generation": {"usd": 0.01, "diem": 0.01},
                    "upscale": {"4x": {"usd": 0.08, "diem": 0.08}},
                },
            },
        },
        {
            "id": "grok-imagine-image-quality",
            "type": "image",
            "created": 1727966436,
            "model_spec": {
                "name": "Grok Imagine High Quality",
                "pricing": {
                    "resolutions": {
                        "1K": {"usd": 0.06, "diem": 0.06},
                        "2K": {"usd": 0.09, "diem": 0.09},
                    },
                    "upscale": {"4x": {"usd": 0.08, "diem": 0.08}},
                },
            },
        },
        {
            "id": "gpt-image-2",
            "type": "image",
            "created": 1727966436,
            "model_spec": {
                "name": "GPT Image 2",
                "constraints": {
                    "defaultResolution": "1K",
                    "resolutions": ["1K", "2K"],
                    "defaultQuality": "medium",
                    "qualities": ["low", "medium", "high"],
                },
                "pricing": {
                    "resolutions": {
                        "1K": {"usd": 0.07, "diem": 0.07},
                        "2K": {"usd": 0.1, "diem": 0.1},
                    },
                    "quality": {
                        "1K": {
                            "low": {"usd": 0.02, "diem": 0.02},
                            "medium": {"usd": 0.07, "diem": 0.07},
                            "high": {"usd": 0.27, "diem": 0.27},
                        },
                        "2K": {
                            "low": {"usd": 0.03, "diem": 0.03},
                            "high": {"usd": 0.5, "diem": 0.5},
                        },
                    },
                },
            },
        },
        {
            "id": "flux-2-max-edit",
            "type": "inpaint",
            "created": 1727966436,
            "model_spec": {
                "name": "FLUX.2 Max Edit",
                "pricing": {
                    "inpaint": {"usd": 0.12, "diem": 0.12},
                    "inputImages": {
                        "included": 1,
                        "additional": {"usd": 0.0345, "diem": 0.0345},
                    },
                },
            },
        },
        {
            "id": "tts-kokoro",
            "type": "tts",
            "created": 1727966436,
            "model_spec": {
                "name": "Kokoro",
                "pricing": {"input": {"usd": 3.5, "diem": 3.5}},
            },
        },
        {
            "id": "unpriced-video",
            "type": "video",
            "created": 1727966436,
            "model_spec": {"name": "Video"},
        },
    ],
}


def _fetch(payload: dict[str, Any] = CATALOG) -> tuple[list[Any], list[dict[str, Any]]]:
    import asyncio

    calls: list[dict[str, Any]] = []
    provider = VeniceUpstreamProvider(api_key="sk-test")
    with patch(
        "routstr.upstream.venice.httpx.AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(payload, calls),
    ):
        models = asyncio.run(provider.fetch_models())
    return models, calls


def test_requests_every_model_family() -> None:
    _, calls = _fetch()
    assert calls[0]["params"] == {"type": "all"}
    assert calls[0]["url"] == "https://api.venice.ai/api/v1/models"
    assert calls[0]["headers"] == {"Authorization": "Bearer sk-test"}


def test_text_pricing_is_per_token() -> None:
    models, _ = _fetch()
    model = next(m for m in models if m.id == "venice-uncensored-1-2")
    assert model.pricing.prompt == pytest.approx(0.2 / 1_000_000)
    assert model.pricing.completion == pytest.approx(0.9 / 1_000_000)
    assert model.pricing.input_cache_read == pytest.approx(0.02 / 1_000_000)
    assert model.pricing.input_cache_write == pytest.approx(0.25 / 1_000_000)
    assert model.context_length == 128000
    assert model.top_provider is not None
    assert model.top_provider.max_completion_tokens == 8192
    assert model.architecture.input_modalities == ["text", "image"]
    assert model.architecture.modality == "text+image->text"


def test_embedding_models_are_listed() -> None:
    models, _ = _fetch()
    model = next(m for m in models if m.id == "text-embedding-bge-m3")
    assert model.architecture.output_modalities == ["embedding"]
    assert model.pricing.prompt == pytest.approx(0.01 / 1_000_000)
    assert model.pricing.completion == 0.0


def test_families_billed_per_clip_are_dropped() -> None:
    """Audio and video return no usage to settle against, so listing them here
    would hand out inference this provider cannot price."""
    models, _ = _fetch()
    ids = {m.id for m in models}
    assert "tts-kokoro" not in ids
    assert "unpriced-video" not in ids


def test_offline_and_unpriced_models_are_dropped() -> None:
    models, _ = _fetch()
    ids = {m.id for m in models}
    assert "offline-model" not in ids
    assert "unpriced-text" not in ids


def _priced_entry(model_id: str, model_type: str, pricing: dict[str, Any]) -> dict:
    return {
        "id": model_id,
        "type": model_type,
        "created": 1727966436,
        "model_spec": {"name": model_id, "pricing": pricing},
    }


@pytest.mark.parametrize(
    "pricing",
    [
        pytest.param({"input": {"usd": 0.2, "diem": 0.2}}, id="missing-output"),
        pytest.param(
            {"input": {"usd": 0.0, "diem": 0.0}, "output": {"usd": 0.0, "diem": 0.0}},
            id="both-zero",
        ),
        pytest.param(
            {"input": {"usd": -0.2, "diem": 0.2}, "output": {"usd": 0.9, "diem": 0.9}},
            id="negative-input",
        ),
        pytest.param(
            {"input": {"usd": 0.2, "diem": 0.2}, "output": {"usd": -0.9, "diem": 0.9}},
            id="negative-output",
        ),
    ],
)
def test_text_models_that_would_bill_free_or_negative_are_dropped(
    pricing: dict[str, Any],
) -> None:
    models, _ = _fetch(
        {"object": "list", "data": [_priced_entry("bad-text", "text", pricing)]}
    )
    assert models == []


def test_embedding_with_only_an_input_price_is_listed() -> None:
    models, _ = _fetch(
        {
            "object": "list",
            "data": [
                _priced_entry("emb", "embedding", {"input": {"usd": 0.05, "diem": 0}})
            ],
        }
    )
    assert [m.id for m in models] == ["emb"]
    assert models[0].pricing.prompt == pytest.approx(0.05 / 1_000_000)
    assert models[0].pricing.completion == 0.0


def test_embedding_with_a_negative_price_is_dropped() -> None:
    models, _ = _fetch(
        {
            "object": "list",
            "data": [
                _priced_entry("emb", "embedding", {"input": {"usd": -0.05, "diem": 0}})
            ],
        }
    )
    assert models == []


def test_text_model_with_one_zero_price_is_listed() -> None:
    """Only both-zero is free; a free prompt with a paid completion is priced."""
    pricing = {"input": {"usd": 0.0, "diem": 0}, "output": {"usd": 0.9, "diem": 0}}
    models, _ = _fetch(
        {"object": "list", "data": [_priced_entry("t", "text", pricing)]}
    )
    assert [m.id for m in models] == ["t"]
    assert models[0].pricing.completion == pytest.approx(0.9 / 1_000_000)


def test_model_name_drops_the_venice_prefix() -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")
    assert provider.transform_model_name("venice/venice-uncensored-1-2") == (
        "venice-uncensored-1-2"
    )
    assert provider.transform_model_name("venice-uncensored-1-2") == (
        "venice-uncensored-1-2"
    )


def test_provider_metadata_pins_the_base_url() -> None:
    metadata = VeniceUpstreamProvider.get_provider_metadata()
    assert metadata["id"] == "venice"
    assert metadata["default_base_url"] == "https://api.venice.ai/api/v1"
    assert metadata["fixed_base_url"] is True


def test_fetch_returns_empty_on_upstream_failure() -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")

    with patch.object(
        VeniceUpstreamProvider,
        "_fetch_provider_models",
        side_effect=RuntimeError("boom"),
    ):
        import asyncio

        assert asyncio.run(provider.fetch_models()) == []


def test_image_models_are_listed() -> None:
    models, _ = _fetch()
    by_id = {m.id: m for m in models}
    assert "venice-sd35" in by_id
    assert by_id["venice-sd35"].architecture.output_modalities == ["image"]
    assert by_id["venice-sd35"].pricing.image_output == pytest.approx(0.01)


def test_image_pricing_uses_worst_case_resolution_not_upscale() -> None:
    models, _ = _fetch()
    by_id = {m.id: m for m in models}
    # 0.09 is the 2K generation price; 0.08 is a separate /image/upscale call.
    assert by_id["grok-imagine-image-quality"].pricing.image_output == pytest.approx(
        0.09
    )
    # inputImages is a per-extra-image surcharge, not the generation price.
    assert by_id["flux-2-max-edit"].pricing.image_output == pytest.approx(0.12)


def test_worst_case_rate_covers_the_quality_table() -> None:
    models, _ = _fetch()
    by_id = {m.id: m for m in models}
    # 0.5 is the 2K/high quality tier, above every resolutions entry.
    assert by_id["gpt-image-2"].pricing.image_output == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("model_id", "body", "expected_usd"),
    [
        # Venice's own resolution label, and the OpenAI size that maps to it.
        ("grok-imagine-image-quality", {"resolution": "1K"}, 0.06),
        ("grok-imagine-image-quality", {"size": "1024x1024"}, 0.06),
        ("grok-imagine-image-quality", {"width": 2048, "height": 1024}, 0.09),
        # Quality wins over the bare resolution price when both are known.
        ("gpt-image-2", {"resolution": "1K", "quality": "low"}, 0.02),
        ("gpt-image-2", {"size": "1024x1536", "quality": "HIGH"}, 0.27),
        ("gpt-image-2", {"resolution": "1K"}, 0.07),
        # No tier named: the upstream's own defaults, not the ceiling.
        ("gpt-image-2", {}, 0.07),
        # Declared but unpriced quality step still resolves through resolution.
        ("gpt-image-2", {"resolution": "2K", "quality": "medium"}, 0.1),
        # A tier the model never declared is billed at the ceiling.
        ("gpt-image-2", {"resolution": "4K"}, 0.5),
        ("gpt-image-2", {"quality": "ultra"}, 0.5),
        # A model with no tiers at all prices everything the same.
        ("venice-sd35", {"resolution": "2K", "quality": "low"}, 0.01),
        ("venice-sd35", {}, 0.01),
    ],
)
def test_per_image_price_narrows_to_the_requested_tier(
    model_id: str, body: dict[str, Any], expected_usd: float
) -> None:
    models, _ = _fetch()
    model = next(m for m in models if m.id == model_id)
    # sats_pricing is 1 sat per USD here, so the tier reads back in USD.
    model = model.copy(update={"sats_pricing": model.pricing})

    assert per_image_sats(model, body) == pytest.approx(expected_usd)


def test_per_image_price_survives_an_unreadable_body() -> None:
    models, _ = _fetch()
    model = next(m for m in models if m.id == "gpt-image-2")
    model = model.copy(update={"sats_pricing": model.pricing})

    # A body naming no tier prices at the model's own defaults.
    assert per_image_sats(model, parse_json_body(b"not json")) == pytest.approx(0.07)
    assert per_image_sats(model, parse_json_body(None)) == pytest.approx(0.07)


def test_image_price_book_is_carried_on_the_model() -> None:
    models, _ = _fetch()
    book = next(m for m in models if m.id == "gpt-image-2").image_pricing

    assert book is not None
    assert book.max_usd == pytest.approx(0.5)
    assert book.default_resolution == "1K"
    assert book.default_quality == "medium"
    assert book.resolutions == ["1K", "2K"]
    assert book.qualities == ["low", "medium", "high"]
    assert {(t.resolution, t.quality, t.usd) for t in book.tiers} == {
        ("1K", None, 0.07),
        ("2K", None, 0.1),
        ("1K", "low", 0.02),
        ("1K", "medium", 0.07),
        ("1K", "high", 0.27),
        ("2K", "low", 0.03),
        ("2K", "high", 0.5),
    }


def test_upscale_factors_are_priced_separately() -> None:
    models, _ = _fetch()
    book = next(m for m in models if m.id == "venice-sd35").image_pricing

    assert book is not None
    assert book.upscale == {"4x": 0.08}
    assert book.upscale_usd("4x") == pytest.approx(0.08)
    # An unnamed factor takes the dearest one rather than under-billing.
    assert book.upscale_usd(None) == pytest.approx(0.08)


def test_models_without_images_carry_no_price_book() -> None:
    models, _ = _fetch()
    by_id = {m.id: m for m in models}

    assert by_id["venice-uncensored-1-2"].image_pricing is None


def test_image_reservation_covers_a_batch_not_a_token_window() -> None:
    models, _ = _fetch()
    provider = VeniceUpstreamProvider(api_key="sk-test", provider_fee=1.0)
    image_model = next(m for m in models if m.id == "venice-sd35")
    priced = provider._apply_provider_fee_to_model(image_model)
    assert priced.pricing.max_cost == pytest.approx(0.04)
    assert priced.pricing.max_prompt_cost == 0.0

    text_model = next(m for m in models if m.id == "venice-uncensored-1-2")
    text_priced = provider._apply_provider_fee_to_model(text_model)
    assert text_priced.pricing.max_cost > 0
