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
    static_image_book,
)
from routstr.upstream.openai import OpenAIUpstreamProvider
from routstr.upstream.openrouter import OpenRouterUpstreamProvider
from routstr.upstream.xai import XAIUpstreamProvider


def _endpoints(*lines: dict[str, Any], supported: dict | None = None) -> dict:
    return {
        "id": "m",
        "endpoints": [
            {
                "provider_name": "P",
                "provider_tag": "p",
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
    assert set(book.endpoints) == {"p"}
    assert book.endpoints["p"].endpoint_tag == "p"
    assert select_image_price_usd(book, {"resolution": "4K"}) == pytest.approx(0.04)


def test_openrouter_variants_are_reserved_at_the_dearest() -> None:
    """No request field reliably names a variant, so none is mapped to one."""
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
    endpoint = book.endpoints["p"]
    assert endpoint.max_usd == pytest.approx(0.10)
    assert endpoint.parameters == {
        "resolution": {"type": "enum", "values": ["2K", "4K"]}
    }
    assert select_image_price_usd(endpoint, {"resolution": "2K"}) == pytest.approx(0.10)


@pytest.mark.parametrize(
    "lines",
    [
        # Quantities the node cannot cap before dispatch.
        [{"billable": "output_image", "unit": "megapixel", "cost_usd": 0.03}],
        [{"billable": "output_image", "unit": "token", "cost_usd": 0.00003}],
        # A metered input next to a flat output.
        [
            {"billable": "output_image", "unit": "image", "cost_usd": 0.04},
            {"billable": "input_text", "unit": "token", "cost_usd": 0.000005},
        ],
        # One billable line in two units.
        [
            {"billable": "output_image", "unit": "image", "cost_usd": 0.04},
            {"billable": "output_image", "unit": "megapixel", "cost_usd": 0.03},
        ],
        # Nothing to bill per image.
        [{"billable": "output_image", "unit": "image", "cost_usd": 0}],
        [{"billable": "input_image", "unit": "image", "cost_usd": 0.01}],
        # Not a rate.
        [{"billable": "output_image", "unit": "image", "cost_usd": "nan"}],
        [{"billable": "output_image", "unit": "image", "cost_usd": -1}],
    ],
)
def test_openrouter_endpoints_without_a_bounded_price_have_no_book(
    lines: list[dict[str, Any]],
) -> None:
    assert openrouter_book_from_endpoints(_endpoints(*lines), "m") is None


def test_openrouter_free_inputs_and_reference_surcharges_are_bounded() -> None:
    book = openrouter_book_from_endpoints(
        _endpoints(
            {"billable": "output_image", "unit": "image", "cost_usd": 0.03},
            {"billable": "input_text", "unit": "token", "cost_usd": 0},
            {"billable": "input_image", "unit": "image", "cost_usd": 0.003},
        ),
        "qwen/qwen-image-3",
    )
    assert book is not None
    assert book.endpoints["p"].input_image_usd == pytest.approx(0.003)
    assert book.input_image_usd == pytest.approx(0.003)


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
            _endpoints({"billable": "output_image", "unit": "image", "cost_usd": 0.03}),
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
    # The Image API had no quotable endpoint for gpt-image-2, so it has no
    # endpoint to pin and is not listed rather than priced off the catalog.
    assert set(by_id) == {"bfl/flux", "openai/gpt-4o"}
    flux = by_id["bfl/flux"]
    assert flux.image_pricing is not None
    assert set(flux.image_pricing.endpoints) == {"p"}
    assert flux.pricing.image_output == pytest.approx(0.03)
    assert by_id["openai/gpt-4o"].image_pricing is None


def test_openai_provider_prices_gpt_image_from_its_token_rate() -> None:
    catalog = [
        dict(entry, id=str(entry["id"]).removeprefix("openai/"))
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


def test_xai_drops_image_models_the_openrouter_feed_prices_per_token() -> None:
    """The feed's ``image_output`` is per token; read per image it bills ~0."""
    catalog = [
        {
            "id": "grok-imagine-image-2.0",
            "name": "Grok Imagine",
            "created": 1,
            "description": "",
            "context_length": 0,
            "architecture": {
                "modality": "text->image",
                "input_modalities": ["text"],
                "output_modalities": ["image"],
                "tokenizer": "Grok",
                "instruct_type": None,
            },
            "pricing": {"prompt": "0", "completion": "0", "image_output": "0.0000096"},
        },
        {
            "id": "grok-4",
            "name": "Grok 4",
            "created": 1,
            "description": "",
            "context_length": 128000,
            "architecture": {
                "modality": "text->text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "Grok",
                "instruct_type": None,
            },
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        },
    ]
    provider = XAIUpstreamProvider(api_key="k")
    with patch(
        "routstr.upstream.xai.async_fetch_openrouter_models", return_value=catalog
    ):
        models = asyncio.run(provider.fetch_models())
    assert [m.id for m in models] == ["grok-4"]


def test_openrouter_tiered_variants_and_reference_surcharge() -> None:
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
    endpoint = book.endpoints["p"]
    # Variants are alternatives no request field reliably names: every
    # request is reserved at the dearest, and ``usage.cost`` settles it.
    assert endpoint.max_usd == pytest.approx(0.08)
    for body in ({}, {"quality": "low", "resolution": "1K"}):
        assert select_image_price_usd(endpoint, body) == pytest.approx(0.08)
    assert endpoint.input_image_usd == pytest.approx(0.01)
    assert endpoint.reference_usd({"input_references": ["a", "b"]}) == pytest.approx(
        0.02
    )
    assert endpoint.reference_usd({}) == 0.0

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
    assert seed.endpoints["p"].max_usd == pytest.approx(0.09)
    assert select_image_price_usd(seed.endpoints["p"], {}) == pytest.approx(0.09)


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
