import json
import os

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

from routstr.upstream import GenericUpstreamProvider
from routstr.upstream.openrouter import OpenRouterUpstreamProvider


def _model(model_id: str = "openai/gpt-4o"):  # type: ignore[no-untyped-def]
    from routstr.payment.models import Architecture, Model, Pricing

    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=128000,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="GPT",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.0, completion=0.0),
    )


def _tool_body() -> dict:
    return {
        "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": "What's the weather?"}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "get_weather", "parameters": {}},
            }
        ],
    }


def _prepare(provider, body: dict) -> dict:  # type: ignore[no-untyped-def]
    out = provider.prepare_request_body(json.dumps(body).encode(), _model())
    assert out is not None
    return json.loads(out)


def test_injects_require_parameters_for_tool_request() -> None:
    data = _prepare(OpenRouterUpstreamProvider(api_key="test"), _tool_body())
    assert data["provider"]["require_parameters"] is True


def test_generic_provider_on_openrouter_url_is_left_alone() -> None:
    # Only OpenRouterUpstreamProvider injects; a generic provider pointed at the
    # same base URL doesn't.
    provider = GenericUpstreamProvider(base_url="https://openrouter.ai/api/v1")
    data = _prepare(provider, _tool_body())
    assert "provider" not in data


def test_no_injection_without_tools() -> None:
    body = {"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    data = _prepare(OpenRouterUpstreamProvider(api_key="test"), body)
    assert "provider" not in data


def test_empty_tools_list_does_not_inject() -> None:
    body = _tool_body()
    body["tools"] = []
    data = _prepare(OpenRouterUpstreamProvider(api_key="test"), body)
    assert "provider" not in data


def test_direct_provider_does_not_inject() -> None:
    provider = GenericUpstreamProvider(base_url="https://api.openai.com/v1")
    data = _prepare(provider, _tool_body())
    assert "provider" not in data


def test_keeps_client_set_require_parameters() -> None:
    body = _tool_body()
    body["provider"] = {"require_parameters": False}
    data = _prepare(OpenRouterUpstreamProvider(api_key="test"), body)
    assert data["provider"]["require_parameters"] is False


def test_preserves_other_provider_fields() -> None:
    body = _tool_body()
    body["provider"] = {"order": ["openai", "azure"]}
    data = _prepare(OpenRouterUpstreamProvider(api_key="test"), body)
    assert data["provider"]["order"] == ["openai", "azure"]
    assert data["provider"]["require_parameters"] is True
