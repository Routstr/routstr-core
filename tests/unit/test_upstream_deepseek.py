"""Unit tests for ``DeepSeekUpstreamProvider``.

DeepSeek is priced from the provider's own peak-rate table, never from litellm
or OpenRouter: litellm's ``deepseek-v4-flash`` entry is stale and OpenRouter
resells below DeepSeek's peak rate, so either would bill under cost. These
tests pin the table prices (including the cache-hit rate), that a model the
table misses imports disabled without consulting the fallback chain, and that
``reasoning_content`` in history reaches DeepSeek untouched — thinking mode
with ``tools`` answers 400 when it is stripped.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from routstr.upstream import upstream_provider_classes
from routstr.upstream.deepseek import DeepSeekUpstreamProvider


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

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self._calls.append({"url": url, "headers": headers})
        return _FakeResponse(self._payload)


# Shape of DeepSeek's ``GET /models``: bare ids, no pricing.
CATALOG: dict[str, Any] = {
    "object": "list",
    "data": [
        {"id": "deepseek-flash", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
    ],
}


async def _fetch(
    catalog: dict[str, Any] = CATALOG,
) -> tuple[dict[str, Any], list[dict[str, Any]], AsyncMock]:
    calls: list[dict[str, Any]] = []
    fallback = AsyncMock(return_value=None)
    provider = DeepSeekUpstreamProvider(api_key="sk-test")
    with (
        patch(
            "routstr.upstream.generic.httpx.AsyncClient",
            lambda *args, **kwargs: _FakeAsyncClient(catalog, calls),
        ),
        patch("routstr.upstream.generic.FallbackPricingResolver.resolve", fallback),
    ):
        models = await provider.fetch_models()
    return {m.id: m for m in models}, calls, fallback


def test_metadata_and_registration() -> None:
    assert DeepSeekUpstreamProvider in upstream_provider_classes
    assert DeepSeekUpstreamProvider.get_provider_metadata() == {
        "id": "deepseek",
        "name": "DeepSeek",
        "default_base_url": "https://api.deepseek.com",
        "fixed_base_url": True,
        "platform_url": "https://platform.deepseek.com/api_keys",
    }


def test_build_from_row_ignores_row_base_url() -> None:
    row = Mock(
        api_key="sk-row", provider_fee=1.05, base_url="https://elsewhere.example"
    )
    provider = DeepSeekUpstreamProvider._build_from_row(row)
    assert provider.api_key == "sk-row"
    assert provider.provider_fee == 1.05
    assert provider.base_url == "https://api.deepseek.com"


def test_litellm_prefix_is_deepseek() -> None:
    provider = DeepSeekUpstreamProvider(api_key="sk-test")
    assert provider.get_litellm_provider_prefix() == "deepseek/"


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("deepseek/deepseek-v4-flash", "deepseek-v4-flash"),
        ("deepseek-v4-flash", "deepseek-v4-flash"),
        ("deepseek/deepseek-flash", "deepseek-flash"),
    ],
)
def test_transform_model_name(model_id: str, expected: str) -> None:
    provider = DeepSeekUpstreamProvider(api_key="sk-test")
    assert provider.transform_model_name(model_id) == expected


def test_provider_field_names_deepseek_not_host() -> None:
    provider = DeepSeekUpstreamProvider(api_key="sk-test")
    payload: dict[str, Any] = {"id": "chatcmpl-1"}
    provider._apply_provider_field(payload)
    assert payload["provider"] == "deepseek"


@pytest.mark.asyncio
async def test_fetch_models_calls_deepseek_models_endpoint_with_key() -> None:
    _, calls, _ = await _fetch()
    assert calls == [
        {
            "url": "https://api.deepseek.com/models",
            "headers": {"Authorization": "Bearer sk-test"},
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_id,prompt,completion,cache_read",
    [
        ("deepseek-flash", 0.30, 1.20, 0.006),
        # Retired alias DeepSeek serves and bills as deepseek-flash.
        ("deepseek-v4-flash", 0.30, 1.20, 0.006),
        ("deepseek-v4-pro", 1.32, 3.96, 0.044),
    ],
)
async def test_table_models_priced_at_peak_rate(
    model_id: str, prompt: float, completion: float, cache_read: float
) -> None:
    models, _, _ = await _fetch()
    model = models[model_id]
    assert model.enabled is True
    assert model.pricing.prompt == pytest.approx(prompt / 1_000_000)
    assert model.pricing.completion == pytest.approx(completion / 1_000_000)
    assert model.pricing.input_cache_read == pytest.approx(cache_read / 1_000_000)
    assert model.context_length == 1_000_000


@pytest.mark.asyncio
async def test_vision_follows_the_model() -> None:
    models, _, _ = await _fetch()
    assert "image" in models["deepseek-flash"].architecture.input_modalities
    assert models["deepseek-v4-pro"].architecture.input_modalities == ["text"]


@pytest.mark.asyncio
async def test_unlisted_model_imports_disabled_without_fallback() -> None:
    """litellm prices ``deepseek-chat``; the provider must not take that price."""
    models, _, fallback = await _fetch()
    model = models["deepseek-chat"]
    assert model.enabled is False
    assert model.pricing.prompt == 0.0
    assert model.pricing.completion == 0.0
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_rate_survives_fee_and_is_not_replaced_by_litellm() -> None:
    """litellm's stale ``deepseek-v4-flash`` cache rate (1.4e-08 in the bundled
    map) must not replace the table's; backfill only fills an absent rate. The
    fee applies to the cache rate like every other component.

    The litellm entry is pinned here because the remote cost map already
    carries the table's rate, which would let an overwrite go unnoticed."""
    models, _, _ = await _fetch()
    provider = DeepSeekUpstreamProvider(api_key="sk-test", provider_fee=1.05)
    stale = {"cache_read_input_token_cost": 1.4e-08}
    with patch("routstr.payment.models.litellm_cost_entry", return_value=stale):
        priced = provider._apply_provider_fee_to_model(models["deepseek-v4-flash"])
    assert priced.pricing.input_cache_read == pytest.approx(0.006e-6 * 1.05)
    assert priced.pricing.prompt == pytest.approx(0.30e-6 * 1.05)
    # A cache hit costs 2% of a miss, not the full input rate.
    assert priced.pricing.input_cache_read / priced.pricing.prompt == pytest.approx(
        0.02
    )


@pytest.mark.asyncio
async def test_reasoning_content_in_history_reaches_upstream() -> None:
    models, _, _ = await _fetch()
    provider = DeepSeekUpstreamProvider(api_key="sk-test")
    messages = [
        {"role": "user", "content": "weather in Paris?"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "Need the weather tool.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "18C"},
    ]
    body = json.dumps(
        {
            "model": "deepseek/deepseek-flash",
            "messages": messages,
            "tools": [{"type": "function", "function": {"name": "get_weather"}}],
        }
    ).encode()
    out = provider.prepare_request_body(body, models["deepseek-flash"])

    assert out is not None
    sent = json.loads(out)
    assert sent["model"] == "deepseek-flash"
    assert sent["messages"] == messages
