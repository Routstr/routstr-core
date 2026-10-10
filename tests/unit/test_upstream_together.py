"""Unit tests for ``TogetherUpstreamProvider.fetch_models``.

Together's ``/models`` prices text per million tokens. Exact image
``price_per_megapixel`` rates are accepted, while non-bounding
``example_price`` values are ignored; otherwise image models are priced from
the published table or the operator's ``provider_settings.image_prices`` and
dropped when none has them.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from routstr.payment.image_pricing import per_image_sats
from routstr.upstream import upstream_provider_classes
from routstr.upstream.together import TogetherUpstreamProvider


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload: Any, calls: list[dict[str, Any]]) -> None:
        self._payload = payload
        self._calls = calls

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self._calls.append({"url": url, "headers": headers})
        return _FakeResponse(self._payload)


CATALOG: list[dict[str, Any]] = [
    {
        "id": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "object": "model",
        "created": 1,
        "type": "chat",
        "display_name": "Llama 3.3 70B",
        "organization": "Meta",
        "context_length": 131072,
        "pricing": {
            "hourly": 0,
            "input": 0.88,
            "output": 0.88,
            "base": 0,
            "finetune": 0,
        },
    },
    {
        "id": "black-forest-labs/FLUX.1-schnell",
        "object": "model",
        "created": 1,
        "type": "image",
        "display_name": "FLUX.1 [schnell]",
        "organization": "Black Forest Labs",
        "pricing": {"hourly": 0, "input": 0, "output": 0, "base": 0, "finetune": 0},
    },
    {
        "id": "black-forest-labs/FLUX.1-kontext-pro",
        "object": "model",
        "created": 1,
        "type": "image",
        "display_name": "FLUX.1 Kontext [pro]",
    },
    {
        "id": "someone/brand-new-image",
        "object": "model",
        "created": 1,
        "type": "image",
    },
    {
        "id": "ByteDance/Seedream-5.0-lite",
        "object": "model",
        "created": 1,
        "type": "image",
        "pricing": {
            "image": {"example_price": 0.035, "example_description": "2K & 3K"}
        },
    },
    {
        "id": "black-forest-labs/FLUX.2-max",
        "object": "model",
        "created": 1,
        "type": "image",
        "pricing": {"image_pixel": {"price_per_megapixel": 0.09, "min_steps": 50}},
    },
    {
        "id": "black-forest-labs/FLUX.2-dev",
        "object": "model",
        "created": 1,
        "type": "image",
        "pricing": {
            "image": {
                "example_price": 0.0154,
                "example_description": "lowest resolution",
            }
        },
    },
    {
        "id": "togethercomputer/m2-bert-80M-8k-retrieval",
        "object": "model",
        "created": 1,
        "type": "embedding",
        "pricing": {"input": 0.008, "output": 0},
    },
    {"id": "some/audio", "object": "model", "created": 1, "type": "audio"},
]


def _fetch(
    payload: Any = CATALOG, image_prices: dict[str, Any] | None = None
) -> tuple[list[Any], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    provider = TogetherUpstreamProvider(api_key="sk-test", image_prices=image_prices)
    with patch(
        "routstr.upstream.together.httpx.AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(payload, calls),
    ):
        models = asyncio.run(provider.fetch_models())
    return models, calls


def test_provider_is_registered() -> None:
    assert TogetherUpstreamProvider in upstream_provider_classes
    assert TogetherUpstreamProvider.get_provider_metadata()["id"] == "together"


def test_requests_the_catalog_with_the_key() -> None:
    _, calls = _fetch()
    assert calls[0]["url"] == "https://api.together.xyz/v1/models"
    assert calls[0]["headers"] == {"Authorization": "Bearer sk-test"}


def test_text_pricing_is_per_token() -> None:
    models, _ = _fetch()
    by_id = {m.id: m for m in models}
    llama = by_id["meta-llama/Llama-3.3-70B-Instruct-Turbo"]
    assert llama.pricing.prompt == pytest.approx(0.88 / 1_000_000)
    assert llama.pricing.completion == pytest.approx(0.88 / 1_000_000)
    assert llama.context_length == 131072
    assert llama.architecture.output_modalities == ["text"]
    assert llama.image_pricing is None


def test_published_image_prices_become_books() -> None:
    models, _ = _fetch()
    by_id = {m.id: m for m in models}
    schnell = by_id["black-forest-labs/FLUX.1-schnell"]
    assert schnell.architecture.output_modalities == ["image"]
    assert schnell.image_pricing is not None
    assert schnell.image_pricing.unit == "megapixel"
    assert schnell.image_pricing.megapixel_usd == pytest.approx(0.0027)
    assert schnell.pricing.image_output == pytest.approx(schnell.image_pricing.max_usd)

    kontext = by_id["black-forest-labs/FLUX.1-kontext-pro"]
    assert kontext.image_pricing is not None
    assert kontext.image_pricing.unit == "megapixel"
    assert kontext.image_pricing.megapixel_usd == pytest.approx(0.04)
    assert kontext.image_pricing.default_steps == 28
    assert kontext.pricing.image_output == pytest.approx(
        kontext.image_pricing.max_usd
    )


def test_exact_catalog_prices_beat_the_table_but_starting_prices_do_not() -> None:
    models, _ = _fetch()
    by_id = {m.id: m for m in models}

    # The table says 0.07/MP; the catalog's exact 0.09/MP wins.
    flux_max = by_id["black-forest-labs/FLUX.2-max"]
    assert flux_max.image_pricing is not None
    assert flux_max.image_pricing.unit == "megapixel"
    assert flux_max.image_pricing.megapixel_usd == pytest.approx(0.09)
    assert flux_max.image_pricing.default_steps == 50

    # ``example_price`` is only a starting price. It must not replace the
    # table's scalable per-megapixel ceiling for a known model.
    flux_dev = by_id["black-forest-labs/FLUX.2-dev"]
    assert flux_dev.image_pricing is not None
    assert flux_dev.image_pricing.unit == "megapixel"
    assert flux_dev.image_pricing.megapixel_usd == pytest.approx(0.0154)

    # An unknown model with only a starting price cannot be bounded safely.
    assert "ByteDance/Seedream-5.0-lite" not in by_id


def test_operator_prices_beat_catalog_prices() -> None:
    models, _ = _fetch(image_prices={"bytedance/seedream-5.0-lite": 0.05})
    by_id = {m.id: m for m in models}
    assert by_id["ByteDance/Seedream-5.0-lite"].pricing.image_output == pytest.approx(
        0.05
    )


def test_unknown_image_models_and_other_families_are_dropped() -> None:
    models, _ = _fetch()
    ids = {m.id for m in models}
    assert "someone/brand-new-image" not in ids
    assert "ByteDance/Seedream-5.0-lite" not in ids
    assert "some/audio" not in ids
    assert "togethercomputer/m2-bert-80M-8k-retrieval" in ids


def test_operator_prices_win_and_price_unknown_models() -> None:
    models, _ = _fetch(
        image_prices={
            "someone/brand-new-image": {"usd": 0.05, "unit": "image"},
            "black-forest-labs/flux.1-kontext-pro": 0.10,
        }
    )
    by_id = {m.id: m for m in models}
    assert by_id["someone/brand-new-image"].pricing.image_output == pytest.approx(0.05)
    assert by_id[
        "black-forest-labs/FLUX.1-kontext-pro"
    ].pricing.image_output == pytest.approx(0.10)


def test_operator_prices_are_read_from_provider_settings() -> None:
    class Row:
        api_key = "k"
        provider_fee = 1.02
        provider_settings = json.dumps(
            {"image_prices": {"X/Y": {"usd": 0.2, "unit": "megapixel"}}}
        )

    provider = TogetherUpstreamProvider._build_from_row(Row())  # type: ignore[arg-type]
    book = provider.image_book("x/y")
    assert book is not None and book.unit == "megapixel"
    assert book.megapixel_usd == pytest.approx(0.2)


def test_megapixel_image_is_reserved_at_the_requested_size() -> None:
    models, _ = _fetch()
    provider = TogetherUpstreamProvider(api_key="k", provider_fee=1.0)
    schnell = next(m for m in models if m.id == "black-forest-labs/FLUX.1-schnell")
    priced = provider._apply_provider_fee_to_model(schnell)
    # Treat the USD book as sats one-to-one for the ratio check.
    priced = priced.copy(update={"sats_pricing": priced.pricing})
    assert per_image_sats(priced, {"width": 1024, "height": 1024}) == pytest.approx(
        0.0027 * 1.048576
    )
    assert per_image_sats(priced, {}) == pytest.approx(0.0027)


def test_catalog_default_steps_scale_megapixel_price_only_above_default() -> None:
    models, _ = _fetch()
    provider = TogetherUpstreamProvider(api_key="k", provider_fee=1.0)
    flux_max = next(m for m in models if m.id == "black-forest-labs/FLUX.2-max")
    priced = provider._apply_provider_fee_to_model(flux_max)
    priced = priced.copy(update={"sats_pricing": priced.pricing})

    default = per_image_sats(priced, {"width": 1024, "height": 1024})
    assert per_image_sats(
        priced, {"width": 1024, "height": 1024, "steps": 25}
    ) == pytest.approx(default)
    assert per_image_sats(
        priced, {"width": 1024, "height": 1024, "steps": 100}
    ) == pytest.approx(default * 2)


def test_empty_catalog_on_error() -> None:
    class Boom:
        async def __aenter__(self) -> "Boom":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("down")

    provider = TogetherUpstreamProvider(api_key="k")
    with patch("routstr.upstream.together.httpx.AsyncClient", lambda *a, **kw: Boom()):
        assert asyncio.run(provider.fetch_models()) == []
