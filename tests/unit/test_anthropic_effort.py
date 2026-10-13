import json

import pytest

from routstr.core.exceptions import UpstreamError
from routstr.payment.models import Architecture, Model, Pricing, Reasoning
from routstr.upstream.anthropic import AnthropicUpstreamProvider
from routstr.upstream.generic import GenericUpstreamProvider


@pytest.fixture
def model() -> Model:
    return Model(
        id="claude-sonnet-5.5",
        name="Sonnet",
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="Claude",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=1e-7, completion=5e-7),
        reasoning=Reasoning(
            mandatory=True,
            default_effort="high",
            supported_efforts=["low", "medium", "high", "xhigh", "max"],
        ),
    )


def prepare(model: Model, **fields: object) -> dict:
    provider = AnthropicUpstreamProvider(api_key="test")
    provider._native_model_names = {model.id: "claude-sonnet-5-5"}
    body = {"model": model.id, "messages": [], "max_tokens": 16, **fields}
    result = provider.prepare_request_body(json.dumps(body).encode(), model)
    assert result is not None
    return json.loads(result)


def test_mandatory_catalog_does_not_inject_native_effort(model: Model) -> None:
    body = prepare(model)
    assert body["model"] == "claude-sonnet-5-5"
    assert "reasoning_effort" not in body
    assert "reasoning" not in body
    assert "output_config" not in body
    assert "thinking" not in body


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("shape", ["reasoning_effort", "reasoning"])
def test_generic_effort_uses_native_output_config(
    model: Model, effort: str, shape: str
) -> None:
    value = effort if shape == "reasoning_effort" else {"effort": effort}
    body = prepare(model, **{shape: value})
    assert body["output_config"] == {"effort": effort}
    assert "reasoning_effort" not in body
    assert "reasoning" not in body
    assert "thinking" not in body


def test_native_output_config_preserves_effort_and_format(model: Model) -> None:
    config = {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    body = prepare(model, output_config=config)
    assert body["output_config"] == config
    assert "reasoning_effort" not in body
    assert "reasoning" not in body


def test_translation_merges_output_config(model: Model) -> None:
    config = {"format": {"type": "json_schema", "schema": {"type": "object"}}}
    body = prepare(model, output_config=config, reasoning_effort="medium")
    assert body["output_config"] == {**config, "effort": "medium"}


@pytest.mark.parametrize(
    "thinking",
    [
        {"type": "adaptive"},
        {"type": "disabled"},
        {"type": "enabled", "budget_tokens": 1024},
    ],
)
def test_native_thinking_is_preserved(model: Model, thinking: dict) -> None:
    body = prepare(model, thinking=thinking)
    assert body["thinking"] == thinking
    assert "reasoning_effort" not in body
    assert "reasoning" not in body
    assert "output_config" not in body


@pytest.mark.parametrize("value", ["none", "minimal", "unknown", 12, {}, True])
def test_invalid_generic_effort_is_rejected(model: Model, value: object) -> None:
    with pytest.raises(UpstreamError, match="Unsupported Anthropic effort") as exc:
        prepare(model, reasoning_effort=value)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "fields",
    [
        {"reasoning_effort": "low", "reasoning": {"effort": "high"}},
        {"reasoning_effort": "low", "output_config": {"effort": "high"}},
    ],
)
def test_conflicting_efforts_are_rejected(model: Model, fields: dict) -> None:
    with pytest.raises(UpstreamError, match="Conflicting Anthropic effort"):
        prepare(model, **fields)


def test_matching_efforts_are_accepted(model: Model) -> None:
    body = prepare(
        model,
        reasoning_effort="medium",
        reasoning={"effort": "medium"},
        output_config={"effort": "medium"},
    )
    assert body["output_config"] == {"effort": "medium"}
    assert "reasoning_effort" not in body
    assert "reasoning" not in body


@pytest.mark.parametrize(
    "fields", [{"reasoning_effort": None}, {"reasoning": {}}, {"reasoning": None}]
)
def test_empty_compatibility_effort_is_removed(model: Model, fields: dict) -> None:
    body = prepare(model, **fields)
    assert "reasoning_effort" not in body
    assert "reasoning" not in body
    assert "output_config" not in body


@pytest.mark.parametrize(
    "value", [{"effort": "high", "exclude": True}, "high", {"max_tokens": 1024}]
)
def test_unsupported_reasoning_settings_are_not_silently_lost(
    model: Model, value: object
) -> None:
    with pytest.raises(UpstreamError, match="reasoning must contain only effort"):
        prepare(model, reasoning=value)


def test_invalid_output_config_is_not_replaced(model: Model) -> None:
    with pytest.raises(UpstreamError, match="output_config must be an object"):
        prepare(model, reasoning_effort="high", output_config=[])


def test_generic_provider_keeps_existing_effort_shape(model: Model) -> None:
    provider = GenericUpstreamProvider(
        base_url="https://example.com/v1", api_key="test"
    )
    result = provider.prepare_request_body(
        json.dumps({"model": model.id, "messages": []}).encode(), model
    )
    assert result is not None
    assert json.loads(result)["reasoning_effort"] == "high"
