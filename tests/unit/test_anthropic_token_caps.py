import json

import pytest

from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.anthropic import AnthropicUpstreamProvider


@pytest.fixture
def model() -> Model:
    return Model(
        id="claude-opus-5.5",
        name="Opus",
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
    )


def prepare(model: Model, **fields: object) -> dict:
    provider = AnthropicUpstreamProvider(api_key="test")
    body = {"model": model.id, "messages": [], **fields}
    result = provider.prepare_request_body(json.dumps(body).encode(), model)
    assert result is not None
    return json.loads(result)


@pytest.mark.parametrize(
    ("max_tokens", "max_completion_tokens", "expected"),
    [(100, 500, 500), (500, 100, 500), ("300", 200, "300")],
)
def test_both_caps_collapse_to_larger_max_tokens(
    model: Model,
    max_tokens: object,
    max_completion_tokens: object,
    expected: object,
) -> None:
    body = prepare(
        model, max_tokens=max_tokens, max_completion_tokens=max_completion_tokens
    )
    assert body["max_tokens"] == expected
    assert "max_completion_tokens" not in body


@pytest.mark.parametrize(
    "fields",
    [
        {"max_tokens": 100},
        {"max_completion_tokens": 100},
        {"max_tokens": 100, "max_completion_tokens": None},
    ],
)
def test_single_cap_is_left_alone(model: Model, fields: dict) -> None:
    assert prepare(model, **fields) == {"model": model.id, "messages": [], **fields}
