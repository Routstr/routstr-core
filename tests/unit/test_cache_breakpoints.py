"""Tests for Anthropic cache-breakpoint injection on forwarded requests.

Anthropic prompt caching is explicit; a client that doesn't recognise a routstr
URL as Anthropic-backed never sends ``cache_control`` markers, so caching never
engages over routstr. ``prepare_request_body`` must stamp the standard
breakpoints for Anthropic-family models while always deferring to client-set
markers and never touching automatic-cache providers.
"""

import json
import os

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

from routstr.upstream import GenericUpstreamProvider
from routstr.upstream.cache_breakpoints import (
    body_has_cache_control,
    inject_anthropic_cache_breakpoints,
    is_explicit_cache_model,
)


def _chat_body() -> dict:
    return {
        "model": "anthropic/claude-sonnet-4.5",
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "Hello"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "a"}},
            {"type": "function", "function": {"name": "b"}},
        ],
    }


# ---------------------------------------------------------------------------
# Model detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("anthropic/claude-sonnet-4.5", True),
        ("claude-haiku-4-5-20251001", True),
        # Alibaba explicit-cache models share Anthropic's wire format
        ("qwen/qwen3-max", True),
        ("qwen/qwen3-coder-plus", True),
        ("deepseek/deepseek-v3.2", True),
        # Automatic-cache providers need no markers
        ("openai/gpt-4o", False),
        ("google/gemini-2.5-flash", False),
        ("deepseek/deepseek-chat", False),
        ("qwen/qwen3.5-plus-02-15", False),  # snapshot, no explicit caching
        (None, False),
    ],
)
def test_is_explicit_cache_model(model_id: str | None, expected: bool) -> None:
    assert is_explicit_cache_model(model_id) is expected


def test_is_explicit_cache_model_uses_fallbacks() -> None:
    # routstr id is opaque but a forwarded/canonical alias reveals the family.
    assert is_explicit_cache_model("model-xyz", None, "anthropic/claude-opus-4.1")


# ---------------------------------------------------------------------------
# Breakpoint placement
# ---------------------------------------------------------------------------


def test_injects_three_breakpoints() -> None:
    data = _chat_body()
    assert inject_anthropic_cache_breakpoints(data) is True

    # system prompt promoted to array form with a marker
    system = data["messages"][0]["content"]
    assert system == [
        {
            "type": "text",
            "text": "You are concise.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # last tool marked
    assert data["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in data["tools"][0]
    # last user message marked
    user = data["messages"][1]["content"]
    assert user[-1]["cache_control"] == {"type": "ephemeral"}


def test_defers_to_client_supplied_cache_control() -> None:
    data = _chat_body()
    data["messages"][1]["content"] = [
        {"type": "text", "text": "Hello", "cache_control": {"type": "ephemeral"}}
    ]
    assert body_has_cache_control(data) is True
    # No additional stamping when the client already controls caching.
    assert inject_anthropic_cache_breakpoints(data) is False
    assert "cache_control" not in data["tools"][-1]


def test_marks_last_text_part_of_array_content() -> None:
    data = _chat_body()
    data["messages"][1]["content"] = [
        {"type": "text", "text": "first"},
        {"type": "image_url", "image_url": {"url": "x"}},
        {"type": "text", "text": "last"},
    ]
    inject_anthropic_cache_breakpoints(data)
    parts = data["messages"][1]["content"]
    assert parts[2]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in parts[0]


def test_noop_without_messages() -> None:
    assert inject_anthropic_cache_breakpoints({"prompt": "x"}) is False


# ---------------------------------------------------------------------------
# prepare_request_body integration
# ---------------------------------------------------------------------------


def _model(model_id: str):  # type: ignore[no-untyped-def]
    from routstr.payment.models import Architecture, Model, Pricing

    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=200000,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="Claude",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.0, completion=0.0),
    )


def _openrouter_provider() -> "GenericUpstreamProvider":
    # OpenRouter endpoint via the generic provider — recognised by base URL.
    return GenericUpstreamProvider(base_url="https://openrouter.ai/api/v1")


@pytest.mark.parametrize(
    "model_id", ["anthropic/claude-sonnet-4.5", "qwen/qwen3-max", "deepseek/deepseek-v3.2"]
)
def test_prepare_request_body_injects_for_explicit_models(model_id: str) -> None:
    provider = _openrouter_provider()
    body = json.dumps(_chat_body()).encode()
    out = provider.prepare_request_body(body, _model(model_id))
    assert out is not None
    data = json.loads(out)
    assert body_has_cache_control(data) is True
    assert data["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_prepare_request_body_skips_for_automatic_provider_model() -> None:
    provider = _openrouter_provider()
    body = json.dumps(_chat_body()).encode()
    out = provider.prepare_request_body(body, _model("openai/gpt-4o"))
    assert out is not None
    data = json.loads(out)
    assert body_has_cache_control(data) is False


def test_prepare_request_body_skips_when_upstream_rejects_markers() -> None:
    # Claude id but a non-OpenRouter/Anthropic upstream → must NOT inject,
    # since the markers could be rejected by an upstream that doesn't accept them.
    from routstr.upstream import GenericUpstreamProvider

    provider = GenericUpstreamProvider(base_url="https://some-gateway.example/v1")
    body = json.dumps(_chat_body()).encode()
    out = provider.prepare_request_body(body, _model("anthropic/claude-sonnet-4.5"))
    assert out is not None
    data = json.loads(out)
    assert body_has_cache_control(data) is False
