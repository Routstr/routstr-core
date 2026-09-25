"""Unit tests for ``VeniceUpstreamProvider.fetch_models``.

Venice answers ``/models`` with only its text catalog unless ``type`` is
passed, which is why the same account configured as a generic upstream sees a
different catalog. These tests pin that query parameter, the per-token pricing
shape, and the families dropped as unpriceable.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

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
                "pricing": {"generation": {"usd": 0.01, "diem": 0.01}},
            },
        },
        {
            "id": "flux-2-max-edit",
            "type": "inpaint",
            "created": 1727966436,
            "model_spec": {
                "name": "FLUX.2 Max Edit",
                "pricing": {"inpaint": {"usd": 0.12, "diem": 0.12}},
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
    """Image, audio and video return no usage to settle against, so listing
    them here would hand out inference this provider cannot price."""
    models, _ = _fetch()
    ids = {m.id for m in models}
    assert "venice-sd35" not in ids
    assert "flux-2-max-edit" not in ids
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
