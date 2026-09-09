"""Per-model reasoning-effort catalog metadata and request mapping."""

from __future__ import annotations

import json
import os
from typing import Any

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routstr.core.db import get_session
from routstr.payment.models import (
    Architecture,
    Model,
    Pricing,
    Reasoning,
    models_router,
)
from routstr.upstream import GenericUpstreamProvider
from routstr.upstream.reasoning_effort import (
    adapt_messages_body_for_litellm,
    apply_reasoning_effort,
    closest_supported_effort,
    extract_requested_effort,
    resolve_effort,
)


def _model(**kwargs: Any) -> Model:
    reasoning = kwargs.pop("reasoning", None)
    return Model(
        id=kwargs.get("id", "openai/gpt-5.6-sol"),
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
        reasoning=reasoning,
    )


SOL_REASONING = {
    "mandatory": False,
    "default_enabled": True,
    "supported_efforts": ["max", "xhigh", "high", "medium", "low", "none"],
    "default_effort": "medium",
}


def test_model_parses_openrouter_reasoning_object() -> None:
    model = Model(
        id="openai/gpt-5.6-sol",
        name="GPT",
        created=0,
        description="",
        context_length=1,
        architecture={
            "modality": "text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tokenizer": "x",
            "instruct_type": None,
        },
        pricing={"prompt": 1e-6, "completion": 1e-6},
        reasoning=SOL_REASONING,
        extra_ignored_field="drop me",
    )
    assert model.reasoning is not None
    assert model.reasoning.supported_efforts == [
        "max",
        "xhigh",
        "high",
        "medium",
        "low",
        "none",
    ]
    dumped = model.dict()
    assert dumped["reasoning"]["supported_efforts"][0] == "max"
    assert dumped["reasoning"]["default_effort"] == "medium"
    assert "extra_ignored_field" not in dumped


def test_non_reasoning_models_omit_the_field() -> None:
    dumped = _model().dict()
    assert "reasoning" not in dumped


def test_malformed_reasoning_is_dropped_not_fatal() -> None:
    model = _model(reasoning=["not", "a", "dict"])
    assert model.reasoning is None
    assert "reasoning" not in model.dict()


def test_closest_effort_maps_minimal_to_low() -> None:
    assert (
        closest_supported_effort(
            "minimal",
            ["max", "xhigh", "high", "medium", "low", "none"],
            default_effort="medium",
        )
        == "low"
    )


def test_closest_effort_maps_max_when_missing() -> None:
    assert (
        closest_supported_effort(
            "max",
            ["high", "medium", "low", "none"],
            default_effort="medium",
        )
        == "high"
    )


def test_mandatory_rejects_none() -> None:
    assert (
        closest_supported_effort(
            "none",
            ["max", "high", "medium", "low", "none"],
            default_effort="high",
            mandatory=True,
        )
        == "high"
    )


def test_missing_request_uses_default() -> None:
    reasoning = Reasoning.parse_obj(SOL_REASONING)
    assert resolve_effort(None, reasoning) == "medium"


def test_extract_prefers_nested_reasoning_effort() -> None:
    assert (
        extract_requested_effort(
            {"reasoning_effort": "low", "reasoning": {"effort": "high"}}
        )
        == "high"
    )


def test_prepare_request_body_rewrites_unsupported_effort() -> None:
    provider = GenericUpstreamProvider(base_url="https://openrouter.ai/api/v1")
    model = _model(reasoning=SOL_REASONING)
    body = json.dumps(
        {
            "model": "openai/gpt-5.6-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "minimal",
        }
    ).encode()
    out = provider.prepare_request_body(body, model)
    assert out is not None
    data = json.loads(out)
    assert data["reasoning_effort"] == "low"


def test_prepare_request_body_rewrites_nested_effort() -> None:
    provider = GenericUpstreamProvider(base_url="https://openrouter.ai/api/v1")
    model = _model(reasoning=SOL_REASONING)
    body = json.dumps(
        {
            "model": "openai/gpt-5.6-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning": {"effort": "minimal", "exclude": False},
        }
    ).encode()
    out = provider.prepare_request_body(body, model)
    data = json.loads(out)
    assert data["reasoning"]["effort"] == "low"
    assert data["reasoning"]["exclude"] is False


def test_prepare_request_body_leaves_plain_chat_alone() -> None:
    provider = GenericUpstreamProvider(base_url="https://openrouter.ai/api/v1")
    model = _model(reasoning=SOL_REASONING)
    payload = {
        "model": "openai/gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hi"}],
    }
    body = json.dumps(payload).encode()
    out = provider.prepare_request_body(body, model)
    assert out == body


def test_apply_injects_default_when_mandatory() -> None:
    data: dict[str, Any] = {
        "model": "anthropic/claude-fable-5.1",
        "messages": [{"role": "user", "content": "hi"}],
    }
    model = _model(
        id="anthropic/claude-fable-5.1",
        reasoning={
            "mandatory": True,
            "supported_efforts": ["max", "xhigh", "high", "medium", "low"],
            "default_effort": "high",
        },
    )
    assert apply_reasoning_effort(data, model) is True
    assert data["reasoning_effort"] == "high"


def test_fee_apply_preserves_reasoning() -> None:
    provider = GenericUpstreamProvider(
        base_url="https://openrouter.ai/api/v1", provider_fee=1.1
    )
    model = _model(reasoning=SOL_REASONING)
    priced = provider._apply_provider_fee_to_model(model)
    assert priced.reasoning is not None
    assert priced.reasoning.supported_efforts == SOL_REASONING["supported_efforts"]


def test_v1_models_includes_reasoning_and_omits_when_absent(
    monkeypatch: Any,
) -> None:
    with_reasoning = _model(id="openai/gpt-5.6-sol", reasoning=SOL_REASONING)
    without = _model(id="openai/gpt-4o")
    unique = {"openai/gpt-5.6-sol": with_reasoning, "openai/gpt-4o": without}

    import routstr.proxy as proxy

    monkeypatch.setattr(proxy, "_unique_models", unique)
    app = FastAPI()
    app.include_router(models_router)
    app.dependency_overrides[get_session] = lambda: None
    response = TestClient(app).get("/v1/models")
    assert response.status_code == 200
    by_id = {row["id"]: row for row in response.json()["data"]}
    assert by_id["openai/gpt-5.6-sol"]["reasoning"]["supported_efforts"] == [
        "max",
        "xhigh",
        "high",
        "medium",
        "low",
        "none",
    ]
    assert "reasoning" not in by_id["openai/gpt-4o"]


def test_messages_thinking_becomes_reasoning_effort() -> None:
    model = _model(reasoning=SOL_REASONING)
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
        "thinking": {"type": "enabled", "effort": "minimal"},
    }
    adapt_messages_body_for_litellm(body, model)
    assert "thinking" not in body
    assert body["reasoning_effort"] == "low"
