import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from routstr.algorithm import create_model_mappings
from routstr.core.db import ModelRow
from routstr.payment.models import Model
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
