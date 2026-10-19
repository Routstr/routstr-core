import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from routstr.algorithm import create_model_mappings
from routstr.core.db import ModelRow
from routstr.payment.models import (
    NATIVE_ANTHROPIC_SUPPORTED_EFFORTS,
    Model,
    apply_native_anthropic_reasoning,
)
from routstr.upstream.anthropic import AnthropicUpstreamProvider
from routstr.upstream.model_paths import _apply_model_visibility


def model_data(model_id: str) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "created": 0,
        "description": "",
        "context_length": 8192,
        "architecture": {
            "modality": "text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tokenizer": "Claude",
            "instruct_type": None,
        },
        "pricing": {
            "prompt": 1e-7,
            "completion": 5e-7,
            "request": 0,
            "image": 0,
            "web_search": 0,
            "internal_reasoning": 0,
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_native_discovery_keeps_pricing_and_public_id() -> None:
    route = respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "claude-haiku-5-5"}], "has_more": False},
        )
    )
    provider = AnthropicUpstreamProvider(api_key="sk-test", workspace_id="wrk_test")
    catalog = [model_data("claude-haiku-5.5"), model_data("unavailable-model")]
    with patch(
        "routstr.upstream.anthropic.async_fetch_openrouter_models",
        AsyncMock(return_value=catalog),
    ):
        models = await provider.fetch_models()

    assert len(models) == 1
    assert models[0].id == "claude-haiku-5.5"
    assert models[0].forwarded_model_id is None
    assert models[0].pricing.prompt == 1e-7
    assert models[0].alias_ids == ["claude-haiku-5-5"]
    assert provider.transform_model_name("claude-haiku-5.5") == "claude-haiku-5-5"
    assert route.calls[0].request.headers["anthropic-workspace-id"] == "wrk_test"
    assert route.calls[0].request.headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
@respx.mock
async def test_native_discovery_follows_pagination_and_dated_mapping() -> None:
    route = respx.get("https://api.anthropic.com/v1/models").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-sonnet-5-5"}],
                    "has_more": True,
                    "last_id": "claude-sonnet-5-5",
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-haiku-4-5-20251001"}],
                    "has_more": False,
                },
            ),
        ]
    )
    provider = AnthropicUpstreamProvider(api_key="sk-test")
    with patch(
        "routstr.upstream.anthropic.async_fetch_openrouter_models",
        AsyncMock(return_value=[model_data("claude-haiku-4.5")]),
    ):
        models = await provider.fetch_models()
    assert models[0].id == "claude-haiku-4.5"
    assert models[0].alias_ids == ["claude-haiku-4-5-20251001"]
    assert route.calls[1].request.url.params["after_id"] == "claude-sonnet-5-5"


@pytest.mark.asyncio
@respx.mock
async def test_native_discovery_failure_does_not_replace_last_mapping() -> None:
    respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(503)
    )
    provider = AnthropicUpstreamProvider(api_key="sk-test")
    provider._native_model_names = {"claude-haiku-5.5": "claude-haiku-5-5"}
    with pytest.raises(httpx.HTTPStatusError):
        await provider.fetch_models()
    assert provider.transform_model_name("claude-haiku-5.5") == "claude-haiku-5-5"


def native_provider() -> AnthropicUpstreamProvider:
    provider = AnthropicUpstreamProvider(api_key="sk-test")
    provider.db_id = 9
    provider._native_model_names = {"claude-haiku-5.5": "claude-haiku-5-5"}
    provider._models_cache = [
        Model(**model_data("claude-haiku-5.5"), alias_ids=["claude-haiku-5-5"])
    ]
    return provider


def sent_model(body: bytes | None) -> str:
    assert body is not None
    return json.loads(body)["model"]


def test_native_and_catalog_aliases_send_native_id() -> None:
    provider = native_provider()
    _, provider_map, public_models = create_model_mappings([provider], {}, set())
    assert list(public_models) == ["claude-haiku-5.5"]
    for alias in ["claude-haiku-5.5", "claude-haiku-5-5"]:
        model, _ = provider_map[alias][0]
        body = json.dumps({"model": alias, "messages": []}).encode()
        assert sent_model(provider.prepare_request_body(body, model)) == (
            "claude-haiku-5-5"
        )
        assert sent_model(provider.prepare_responses_request_body(body, model)) == (
            "claude-haiku-5-5"
        )


def saved_override() -> ModelRow:
    data = model_data("claude-haiku-5.5")
    data["architecture"] = json.dumps(data["architecture"])
    data["pricing"] = json.dumps({**data["pricing"], "prompt": 2e-7})
    return ModelRow(**data, upstream_provider_id=9, forwarded_model_id="my-haiku")


def test_saved_override_keeps_price_client_alias_and_native_request() -> None:
    provider = native_provider()
    _, provider_map, public_models = create_model_mappings(
        [provider],
        overrides_by_key={("claude-haiku-5.5", 9): (saved_override(), 1.0)},
        disabled_model_keys=set(),
    )
    assert len(public_models) == 1
    assert next(iter(public_models.values())).forwarded_model_id == "my-haiku"
    for alias in ["my-haiku", "claude-haiku-5.5"]:
        assert len(provider_map[alias]) == 1
        model, _ = provider_map[alias][0]
        assert model.pricing.prompt == 2e-7
        body = json.dumps({"model": alias, "messages": []}).encode()
        assert sent_model(provider.prepare_request_body(body, model)) == (
            "claude-haiku-5-5"
        )


def test_disabled_catalog_model_stays_hidden() -> None:
    _, provider_map, public_models = create_model_mappings(
        [native_provider()],
        overrides_by_key={},
        disabled_model_keys={("claude-haiku-5.5", 9)},
    )
    assert provider_map == {}
    assert public_models == {}
    assert (
        _apply_model_visibility(native_provider(), {}, {("claude-haiku-5.5", 9)}) == []
    )


@pytest.mark.asyncio
@respx.mock
async def test_invalid_pagination_does_not_loop() -> None:
    respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "claude-haiku-5-5"}], "has_more": True}
        )
    )
    with pytest.raises(ValueError, match="pagination cursor"):
        await AnthropicUpstreamProvider(api_key="sk-test").fetch_models()


# --- published reasoning-effort metadata ---------------------------------------
#
# The native provider accepts a fixed effort domain in ``output_config.effort``
# but its catalog entries come from OpenRouter, which leaves ``reasoning`` null
# for several Claude models. These tests pin the domain published to clients.


class _ProviderRow:
    """Just enough of ``UpstreamProviderRow`` for ``refresh_models_cache``."""

    def __init__(self, pk: int, provider_type: str = "anthropic") -> None:
        self.id = pk
        self.enabled = True
        self.provider_type = provider_type
        self.provider_fee = 1.0
        self.provider_settings = None


def _patch_refresh(
    monkeypatch: pytest.MonkeyPatch,
    provider: AnthropicUpstreamProvider,
    models: list[Model],
) -> None:
    row = _ProviderRow(provider.db_id or 0, provider.provider_type or "anthropic")

    class _Session:
        async def get(self, *_args: Any) -> _ProviderRow:
            return row

    @asynccontextmanager
    async def _session() -> AsyncIterator[_Session]:
        yield _Session()

    monkeypatch.setattr("routstr.upstream.base.create_session", _session)
    monkeypatch.setattr("routstr.upstream.base.list_models", AsyncMock(return_value=[]))
    monkeypatch.setattr(provider, "fetch_models", AsyncMock(return_value=models))


@pytest.mark.asyncio
async def test_refresh_publishes_effort_domain_for_native_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AnthropicUpstreamProvider(api_key="sk-test")
    provider.db_id = 9
    _patch_refresh(monkeypatch, provider, [Model(**model_data("claude-haiku-5.5"))])

    await provider.refresh_models_cache()

    (published,) = provider.get_cached_models()
    assert published.reasoning is not None
    assert published.reasoning.mandatory is False
    assert published.reasoning.supported_efforts == list(
        NATIVE_ANTHROPIC_SUPPORTED_EFFORTS
    )
    # Serialized the way the rest of the payload does: unset levels are omitted,
    # not published as nulls.
    assert published.dict()["reasoning"] == {
        "mandatory": False,
        "supported_efforts": ["low", "medium", "high", "xhigh", "max"],
    }


@pytest.mark.asyncio
async def test_refresh_keeps_catalog_reasoning_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenRouter-shaped metadata is authoritative; the fill only adds."""
    catalog_reasoning = {
        "mandatory": True,
        "default_enabled": True,
        "supported_efforts": ["max", "high", "low", "none"],
        "default_effort": "high",
    }
    provider = AnthropicUpstreamProvider(api_key="sk-test")
    provider.db_id = 9
    _patch_refresh(
        monkeypatch,
        provider,
        [Model(**model_data("claude-opus-5.5"), reasoning=catalog_reasoning)],
    )

    await provider.refresh_models_cache()

    (published,) = provider.get_cached_models()
    assert published.dict()["reasoning"] == catalog_reasoning


def test_effort_domain_is_scoped_to_the_native_anthropic_provider() -> None:
    """``claude-*`` ids on other paths keep their own (or no) metadata."""
    model = Model(**model_data("claude-fable-5.1"))
    for provider_type in (None, "openrouter", "generic", "tinfoil", "litellm"):
        assert apply_native_anthropic_reasoning(model, provider_type) is model
    filled = apply_native_anthropic_reasoning(model, "anthropic")
    assert filled.reasoning is not None
    assert filled.reasoning.supported_efforts == list(
        NATIVE_ANTHROPIC_SUPPORTED_EFFORTS
    )
    assert "reasoning" not in model.dict()


def test_partial_catalog_reasoning_keeps_its_other_fields() -> None:
    model = Model(**model_data("claude-sonnet-5.5"), reasoning={"mandatory": True})
    filled = apply_native_anthropic_reasoning(model, "anthropic")
    assert filled.reasoning is not None
    assert filled.reasoning.mandatory is True
    assert filled.reasoning.supported_efforts == list(
        NATIVE_ANTHROPIC_SUPPORTED_EFFORTS
    )


@pytest.mark.asyncio
async def test_public_catalog_publishes_effort_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AnthropicUpstreamProvider(api_key="sk-test")
    provider.db_id = 9
    _patch_refresh(
        monkeypatch,
        provider,
        [Model(**model_data("claude-haiku-5.5"), alias_ids=["claude-haiku-5-5"])],
    )

    await provider.refresh_models_cache()
    _, _, public_models = create_model_mappings([provider], {}, set())

    advertised = public_models["claude-haiku-5.5"]
    assert advertised.reasoning is not None
    assert advertised.dict()["reasoning"] == {
        "mandatory": False,
        "supported_efforts": ["low", "medium", "high", "xhigh", "max"],
    }


def test_saved_override_keeps_effort_domain() -> None:
    """Rows have no reasoning column, so the override path must re-publish it."""
    _, provider_map, public_models = create_model_mappings(
        [native_provider()],
        overrides_by_key={("claude-haiku-5.5", 9): (saved_override(), 1.0)},
        disabled_model_keys=set(),
    )

    published = next(iter(public_models.values()))
    assert published.reasoning is not None
    assert published.reasoning.supported_efforts == list(
        NATIVE_ANTHROPIC_SUPPORTED_EFFORTS
    )
    override_model, _ = provider_map["my-haiku"][0]
    assert override_model.reasoning is not None
    assert override_model.reasoning.supported_efforts == list(
        NATIVE_ANTHROPIC_SUPPORTED_EFFORTS
    )
