"""Price books built for catalogs that publish none per model.

OpenRouter's Image API itemises billable lines per endpoint; OpenAI meters
image output tokens; Together lists nothing about images at all. Each builder
must produce a book whose unit matches the upstream's metering and whose
ceiling covers the dearest request the model accepts.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from routstr.payment.image_pricing import per_image_sats, select_image_price_usd
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.image_catalog import (
    attach_image_books,
    openai_image_book,
    openrouter_book_from_endpoints,
    openrouter_book_from_pricing,
    static_image_book,
)
from routstr.upstream.openai import OpenAIUpstreamProvider
from routstr.upstream.openrouter import OpenRouterUpstreamProvider


def _endpoints(*lines: dict[str, Any], supported: dict | None = None) -> dict:
    return {
        "id": "m",
        "endpoints": [
            {
                "provider_name": "P",
                "supported_parameters": supported or {},
                "pricing": list(lines),
            }
        ],
    }


def test_openrouter_flat_per_image_book() -> None:
    book = openrouter_book_from_endpoints(
        _endpoints(
            {"billable": "output_image", "unit": "image", "cost_usd": 0.04},
            {"billable": "input_image", "unit": "image", "cost_usd": 0},
        ),
        "seed/x",
    )
    assert book is not None
    assert book.unit == "image"
    assert book.max_usd == pytest.approx(0.04)
    assert book.trust_upstream_cost is True
    assert select_image_price_usd(book, {"resolution": "4K"}) == pytest.approx(0.04)


def test_openrouter_resolution_variants_become_tiers() -> None:
    book = openrouter_book_from_endpoints(
        _endpoints(
            {
                "billable": "output_image",
                "unit": "image",
                "cost_usd": 0.05,
                "variant": "2k",
            },
            {
                "billable": "output_image",
                "unit": "image",
                "cost_usd": 0.10,
                "variant": "4k",
            },
            supported={"resolution": {"type": "enum", "values": ["2K", "4K"]}},
        ),
        "seed/y",
    )
    assert book is not None
    assert book.max_usd == pytest.approx(0.10)
    assert book.default_resolution == "2K"
    assert book.resolutions == ["2K", "4K"]
    assert select_image_price_usd(book, {}) == pytest.approx(0.05)
    assert select_image_price_usd(book, {"resolution": "4K"}) == pytest.approx(0.10)
    # An undeclared class is billed at the ceiling.
    assert select_image_price_usd(book, {"resolution": "8K"}) == pytest.approx(0.10)


def test_openrouter_megapixel_book() -> None:
    book = openrouter_book_from_endpoints(
        _endpoints({"billable": "output_image", "unit": "megapixel", "cost_usd": 0.03}),
        "bfl/flux",
    )
    assert book is not None
    assert book.unit == "megapixel"
    assert book.megapixel_usd == pytest.approx(0.03)
    # No declared resolutions: reserved up to the 2K class.
    assert book.max_usd == pytest.approx(0.12)
    assert [t.resolution for t in book.tiers] == ["512", "1K", "2K"]


def test_openrouter_token_book_for_gpt_image_uses_the_documented_estimates() -> None:
    book = openrouter_book_from_endpoints(
        _endpoints(
            {"billable": "input_image", "unit": "token", "cost_usd": 0.000008},
            {"billable": "input_text", "unit": "token", "cost_usd": 0.000005},
            {"billable": "output_image", "unit": "token", "cost_usd": 0.00003},
            supported={"quality": {"type": "enum", "values": ["auto", "low", "high"]}},
        ),
        "openai/gpt-image-2",
    )
    assert book is not None
    assert book.unit == "token"
    assert book.output_token_usd == pytest.approx(0.00003)
    assert book.input_text_token_usd == pytest.approx(0.000005)
    assert book.input_image_token_usd == pytest.approx(0.000008)
    assert book.trust_upstream_cost is True
    assert select_image_price_usd(book, {}) == pytest.approx(0.053)
    assert select_image_price_usd(book, {"quality": "high"}) == pytest.approx(0.211)
    assert book.max_usd > 0.211


def test_openrouter_token_book_for_an_undocumented_model_is_estimated() -> None:
    book = openrouter_book_from_endpoints(
        _endpoints({"billable": "output_image", "unit": "token", "cost_usd": 0.00001}),
        "someone/new-image",
    )
    assert book is not None
    assert book.unit == "token"
    assert book.max_usd == pytest.approx(0.00001 * 8192)
    assert select_image_price_usd(book, {}) == pytest.approx(book.max_usd)


def test_openrouter_catalog_fallback_reads_image_output_not_image() -> None:
    assert openrouter_book_from_pricing({"image": "0.01"}, "x-ai/grok") is None
    book = openrouter_book_from_pricing(
        {"prompt": "0.000008", "image_output": "0.00003", "image": "0.01"},
        "openai/gpt-image-1",
    )
    assert book is not None
    assert book.unit == "token"
    assert book.output_token_usd == pytest.approx(0.00003)


def test_openai_book_per_family() -> None:
    pricing = {
        "prompt": "0.000008",
        "image_output": "0.00003",
        "image_token": "0.000008",
    }
    two = openai_image_book("gpt-image-2", pricing)
    assert two is not None and two.qualities == ["low", "medium", "high", "auto"]
    assert two.trust_upstream_cost is False
    mini = openai_image_book("gpt-image-1-mini", pricing)
    assert mini is not None and select_image_price_usd(mini, {}) == pytest.approx(0.015)
    flare = openai_image_book("gpt-image-2.5-flare", pricing)
    assert flare is not None
    assert select_image_price_usd(flare, {"quality": "max"}) == pytest.approx(0.211 * 4)
    assert openai_image_book("gpt-4o", pricing) is None
    assert openai_image_book("gpt-image-2", {"prompt": "0.000008"}) is None


def test_static_books() -> None:
    flat = static_image_book(0.04)
    assert flat is not None and flat.unit == "image" and flat.max_usd == 0.04
    area = static_image_book(0.025, "megapixel", ["1K", "2K", "4K"])
    assert area is not None and area.unit == "megapixel"
    assert area.max_usd == pytest.approx(0.4)
    assert static_image_book(0.0) is None


def _image_model(model_id: str) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=0,
        architecture=Architecture(
            modality="text->image",
            input_modalities=["text"],
            output_modalities=["image"],
            tokenizer="Unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.000008, completion=0.000008),
    )


def test_attach_drops_unpriced_image_models_and_sets_the_ceiling() -> None:
    priced = _image_model("a")
    unpriced = _image_model("b")
    text = priced.copy(
        update={
            "id": "t",
            "architecture": Architecture(
                modality="text->text",
                input_modalities=["text"],
                output_modalities=["text"],
                tokenizer="Unknown",
                instruct_type=None,
            ),
        }
    )
    book = static_image_book(0.04)
    assert book is not None
    kept = attach_image_books([priced, unpriced, text], {"a": book}, source="t")
    assert [m.id for m in kept] == ["a", "t"]
    assert kept[0].pricing.image_output == pytest.approx(0.04)
    assert kept[0].image_pricing == book
    assert kept[1].image_pricing is None


OPENROUTER_CATALOG = [
    {
        "id": "openai/gpt-image-2",
        "name": "GPT Image 2",
        "created": 1,
        "description": "",
        "context_length": 0,
        "architecture": {
            "modality": "text+image->image",
            "input_modalities": ["text", "image"],
            "output_modalities": ["image"],
            "tokenizer": "GPT",
            "instruct_type": None,
        },
        "pricing": {
            "prompt": "0.000008",
            "completion": "0.000008",
            "image_output": "0.00003",
        },
    },
    {
        "id": "bfl/flux",
        "name": "Flux",
        "created": 1,
        "description": "",
        "context_length": 0,
        "architecture": {
            "modality": "text->image",
            "input_modalities": ["text"],
            "output_modalities": ["image"],
            "tokenizer": "Other",
            "instruct_type": None,
        },
        "pricing": {"prompt": "0", "completion": "0", "image_output": "0.00001"},
    },
    {
        "id": "openai/gpt-4o",
        "name": "GPT-4o",
        "created": 1,
        "description": "",
        "context_length": 128000,
        "architecture": {
            "modality": "text->text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tokenizer": "GPT",
            "instruct_type": None,
        },
        "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
    },
]


def test_openrouter_provider_prices_image_models_from_the_image_api() -> None:
    async def fake_books(ids: list[str], **_: Any) -> dict:
        assert ids == ["openai/gpt-image-2", "bfl/flux"]
        book = openrouter_book_from_endpoints(
            _endpoints(
                {"billable": "output_image", "unit": "megapixel", "cost_usd": 0.03}
            ),
            "bfl/flux",
        )
        return {"bfl/flux": book}

    provider = OpenRouterUpstreamProvider(api_key="k")
    with (
        patch(
            "routstr.upstream.openrouter.async_fetch_openrouter_models",
            return_value=OPENROUTER_CATALOG,
        ),
        patch("routstr.upstream.openrouter.fetch_openrouter_image_books", fake_books),
    ):
        models = asyncio.run(provider.fetch_models())
    by_id = {m.id: m for m in models}
    assert set(by_id) == {"openai/gpt-image-2", "bfl/flux", "openai/gpt-4o"}
    assert by_id["bfl/flux"].image_pricing is not None
    assert by_id["bfl/flux"].image_pricing.unit == "megapixel"
    # The Image API had nothing for gpt-image-2, so the catalog's token rate stands in.
    gpt = by_id["openai/gpt-image-2"]
    assert gpt.image_pricing is not None and gpt.image_pricing.unit == "token"
    assert gpt.image_pricing.trust_upstream_cost is True
    assert gpt.pricing.image_output == pytest.approx(gpt.image_pricing.max_usd)
    assert by_id["openai/gpt-4o"].image_pricing is None


def test_openai_provider_prices_gpt_image_from_its_token_rate() -> None:
    catalog = [
        dict(entry, id=entry["id"].removeprefix("openai/"))
        for entry in OPENROUTER_CATALOG[:1]
    ]
    provider = OpenAIUpstreamProvider(api_key="k")
    with patch(
        "routstr.upstream.openai.async_fetch_openrouter_models", return_value=catalog
    ):
        models = asyncio.run(provider.fetch_models())
    assert len(models) == 1
    model = models[0]
    assert model.id == "gpt-image-2"
    assert model.image_pricing is not None
    assert model.image_pricing.unit == "token"
    assert model.image_pricing.trust_upstream_cost is False
    priced = provider._apply_provider_fee_to_model(model)
    # A medium 1K request reserves the documented estimate, not the ceiling.
    sats = priced.copy(
        update={
            "sats_pricing": priced.pricing.copy(
                update={"image_output": priced.pricing.image_output}
            )
        }
    )
    assert per_image_sats(sats, {"quality": "medium"}) == pytest.approx(0.053 * 1.01)


def test_openrouter_quality_resolution_variants_and_reference_surcharge() -> None:
    book = openrouter_book_from_endpoints(
        _endpoints(
            {"billable": "input_image", "unit": "image", "cost_usd": 0.01},
            {
                "billable": "output_image",
                "unit": "image",
                "cost_usd": 0.04,
                "variant": "low_1k",
            },
            {
                "billable": "output_image",
                "unit": "image",
                "cost_usd": 0.06,
                "variant": "low_2k",
            },
            {
                "billable": "output_image",
                "unit": "image",
                "cost_usd": 0.06,
                "variant": "medium_1k",
            },
            {
                "billable": "output_image",
                "unit": "image",
                "cost_usd": 0.08,
                "variant": "medium_2k",
            },
            supported={
                "resolution": {"type": "enum", "values": ["1K", "2K"]},
                "quality": {"type": "enum", "values": ["low", "medium"]},
            },
        ),
        "x-ai/grok-imagine-image-2.0",
    )
    assert book is not None
    assert book.max_usd == pytest.approx(0.08)
    assert book.input_image_usd == pytest.approx(0.01)
    assert (book.default_resolution, book.default_quality) == ("1K", "low")
    assert select_image_price_usd(book, {}) == pytest.approx(0.04)
    assert select_image_price_usd(book, {"quality": "medium"}) == pytest.approx(0.06)
    assert select_image_price_usd(
        book, {"resolution": "2K", "quality": "medium"}
    ) == pytest.approx(0.08)
    assert book.reference_usd({"input_references": ["a", "b"]}) == pytest.approx(0.02)
    assert book.reference_usd({}) == 0.0

    seed = openrouter_book_from_endpoints(
        _endpoints(
            {"billable": "output_image", "unit": "image", "cost_usd": 0.045},
            {
                "billable": "output_image",
                "unit": "image",
                "cost_usd": 0.09,
                "variant": "high_resolution",
            },
            {"billable": "input_image", "unit": "image", "cost_usd": 0.003},
        ),
        "bytedance-seed/seedream-5-0-pro",
    )
    assert seed is not None
    # The untiered line prices a plain request; a size it did not name goes
    # to the ceiling, since that is what ``high_resolution`` most likely is.
    assert select_image_price_usd(seed, {}) == pytest.approx(0.045)
    assert select_image_price_usd(seed, {"resolution": "4K"}) == pytest.approx(0.09)
    assert seed.max_usd == pytest.approx(0.09)


def test_attach_carries_input_and_output_image_prices() -> None:
    book = openrouter_book_from_endpoints(
        _endpoints(
            {"billable": "output_image", "unit": "image", "cost_usd": 0.04},
            {"billable": "input_image", "unit": "image", "cost_usd": 0.003},
        ),
        "m",
    )
    assert book is not None
    (kept,) = attach_image_books([_image_model("m")], {"m": book}, source="t")
    assert kept.pricing.image_output == pytest.approx(0.04)
    assert kept.pricing.image == pytest.approx(0.003)
