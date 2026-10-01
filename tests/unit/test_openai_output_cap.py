"""OpenAI reasoning models get ``max_completion_tokens`` before the request is sent."""

from __future__ import annotations

import json
import os

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

import pytest

from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream import GenericUpstreamProvider
from routstr.upstream.openai import OpenAIUpstreamProvider


def _model(model_id: str) -> Model:
    return Model(
        id=model_id,
        name="test",
        created=0,
        description="",
        context_length=128000,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="x",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.0, completion=0.0),
    )


def _chat(model_id: str, **fields: object) -> bytes:
    return json.dumps(
        {"model": model_id, "messages": [{"role": "user", "content": "hi"}], **fields}
    ).encode()


def _prepare(provider: object, model_id: str, body: bytes) -> dict:
    out = provider.prepare_request_body(body, _model(model_id))  # type: ignore[attr-defined]
    assert out is not None
    return json.loads(out)


@pytest.mark.parametrize(
    "model_id", ["gpt-5.6-sol", "openai/gpt-6-sol", "openai/gpt-5", "o3", "o4-mini"]
)
def test_reasoning_model_max_tokens_is_renamed(model_id: str) -> None:
    provider = OpenAIUpstreamProvider(api_key="k")
    data = _prepare(provider, model_id, _chat(model_id, max_tokens=300))
    assert data["max_completion_tokens"] == 300
    assert "max_tokens" not in data


@pytest.mark.parametrize("model_id", ["gpt-4o", "openai/gpt-4.1"])
def test_non_reasoning_model_keeps_max_tokens(model_id: str) -> None:
    provider = OpenAIUpstreamProvider(api_key="k")
    data = _prepare(provider, model_id, _chat(model_id, max_tokens=300))
    assert data["max_tokens"] == 300
    assert "max_completion_tokens" not in data


def test_both_caps_set_is_left_for_upstream() -> None:
    provider = OpenAIUpstreamProvider(api_key="k")
    data = _prepare(
        provider,
        "gpt-5.6-sol",
        _chat("gpt-5.6-sol", max_tokens=300, max_completion_tokens=200),
    )
    assert data["max_tokens"] == 300
    assert data["max_completion_tokens"] == 200


def test_non_chat_body_is_untouched() -> None:
    provider = OpenAIUpstreamProvider(api_key="k")
    body = json.dumps({"model": "gpt-5.6-sol", "input": "hi", "max_tokens": 5}).encode()
    data = _prepare(provider, "gpt-5.6-sol", body)
    assert data["max_tokens"] == 5
    assert "max_completion_tokens" not in data


def test_other_upstreams_keep_max_tokens() -> None:
    provider = GenericUpstreamProvider(base_url="http://test", api_key="k")
    data = _prepare(provider, "gpt-5.6-sol", _chat("gpt-5.6-sol", max_tokens=300))
    assert data["max_tokens"] == 300
    assert "max_completion_tokens" not in data
