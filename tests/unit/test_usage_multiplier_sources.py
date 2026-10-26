import httpx
import pytest

from routstr.core.settings import settings
from routstr.payment.models import Model
from routstr.upstream import usage_multiplier_sources
from routstr.upstream.anthropic import AnthropicUpstreamProvider
from routstr.upstream.usage_multiplier_sources import (
    OpenRouterEndpointsSource,
    derive_multipliers,
    get_usage_multiplier_source,
)


def _endpoint(tag: str, prompt: str, completion: str) -> dict:
    return {"tag": tag, "pricing": {"prompt": prompt, "completion": completion}}


# Tags and prices as OpenRouter listed them for anthropic/claude-opus-5.5.
OPUS_5_5_ENDPOINTS = [
    _endpoint("amazon-bedrock", "0.000004", "0.00002"),
    _endpoint("azure/global", "0.000004", "0.00002"),
    _endpoint("google-vertex/global", "0.000004", "0.00002"),
    _endpoint("anthropic", "0.000004", "0.00002"),
    _endpoint("amazon-bedrock/eu-west-1", "0.0000044", "0.000022"),
    _endpoint("amazon-bedrock/us-east-1", "0.0000044", "0.000022"),
    _endpoint("azure/us", "0.0000044", "0.000022"),
    _endpoint("google-vertex/us", "0.0000044", "0.000022"),
    _endpoint("google-vertex/europe", "0.0000044", "0.000022"),
    _endpoint("anthropic/fast", "0.000008", "0.00004"),
]


def test_derives_fast_and_us_from_openrouter_endpoints() -> None:
    assert derive_multipliers(OPUS_5_5_ENDPOINTS, "anthropic") == {
        "speed": {"fast": 2.0},
        "inference_geo": {"us": 1.1},
    }


def test_no_fast_endpoint_means_no_fast_multiplier() -> None:
    endpoints = [e for e in OPUS_5_5_ENDPOINTS if e["tag"] != "anthropic/fast"]

    assert "speed" not in derive_multipliers(endpoints, "anthropic")


def test_own_us_endpoint_wins_over_other_hosts() -> None:
    endpoints = [
        *OPUS_5_5_ENDPOINTS,
        _endpoint("anthropic/us", "0.0000048", "0.000024"),
    ]

    assert derive_multipliers(endpoints, "anthropic")["inference_geo"] == {"us": 1.2}


def test_disagreeing_hosts_yield_no_us_multiplier() -> None:
    endpoints = [
        _endpoint("anthropic", "0.000004", "0.00002"),
        _endpoint("azure", "0.000004", "0.00002"),
        _endpoint("azure/us", "0.0000044", "0.000022"),
        _endpoint("google-vertex/global", "0.000004", "0.00002"),
        _endpoint("google-vertex/us", "0.000006", "0.00003"),
    ]

    assert "inference_geo" not in derive_multipliers(endpoints, "anthropic")


def test_pair_whose_rates_scale_differently_is_rejected() -> None:
    endpoints = [
        _endpoint("anthropic", "0.000004", "0.00002"),
        _endpoint("anthropic/fast", "0.000008", "0.00003"),
    ]

    assert derive_multipliers(endpoints, "anthropic") == {}


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        return self.routes.get(request.url.path, httpx.Response(404))


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _Transport:
    transport = _Transport(
        {
            "/api/v1/models/anthropic/claude-opus-5.5/endpoints": httpx.Response(
                200, json={"data": {"endpoints": OPUS_5_5_ENDPOINTS}}
            ),
        }
    )
    real_client = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(usage_multiplier_sources.httpx, "AsyncClient", client)
    return transport


@pytest.mark.asyncio
async def test_source_omits_failed_models_and_caches_answers(
    transport: _Transport,
) -> None:
    now = [0.0]
    source = OpenRouterEndpointsSource("anthropic", clock=lambda: now[0])

    first = await source.fetch(["anthropic/claude-opus-5.5", "anthropic/claude-gone"])
    assert first == {
        "anthropic/claude-opus-5.5": {
            "speed": {"fast": 2.0},
            "inference_geo": {"us": 1.1},
        }
    }

    calls = len(transport.calls)
    await source.fetch(["anthropic/claude-opus-5.5"])
    assert len(transport.calls) == calls

    now[0] = usage_multiplier_sources.ENDPOINTS_CACHE_TTL_SECONDS + 1
    await source.fetch(["anthropic/claude-opus-5.5"])
    assert len(transport.calls) == calls + 1


def test_source_is_selected_by_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "usage_multiplier_source", "none")
    assert get_usage_multiplier_source("anthropic") is None

    monkeypatch.setattr(settings, "usage_multiplier_source", "openrouter")
    assert isinstance(
        get_usage_multiplier_source("anthropic"), OpenRouterEndpointsSource
    )


def _model(model_id: str) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=200000,
        architecture={
            "modality": "text->text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tokenizer": "Claude",
        },
        pricing={"prompt": 0.000004, "completion": 0.00002},
    )


@pytest.mark.asyncio
async def test_anthropic_keeps_last_known_multipliers_when_lookup_fails(
    transport: _Transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "usage_multiplier_source", "openrouter")
    monkeypatch.setattr(usage_multiplier_sources, "_instances", {})
    provider = AnthropicUpstreamProvider.__new__(AnthropicUpstreamProvider)
    stale = _model("claude-gone")
    stale.usage_multipliers = {"speed": {"fast": 2.0}}
    provider._models_cache = [stale]

    fresh = [_model("claude-opus-5.5"), _model("claude-gone")]
    await provider._attach_usage_multipliers(fresh)

    assert fresh[0].usage_multipliers == {
        "speed": {"fast": 2.0},
        "inference_geo": {"us": 1.1},
    }
    assert fresh[1].usage_multipliers == {"speed": {"fast": 2.0}}


def test_catalog_omits_unset_multipliers() -> None:
    assert "usage_multipliers" not in _model("claude-opus-4.5").dict()
