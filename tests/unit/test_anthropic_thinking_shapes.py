"""Thinking shapes the native Anthropic API rejects are rewritten before forwarding."""

import json

import pytest

from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.anthropic import AnthropicUpstreamProvider


def make_model(model_id: str) -> Model:
    return Model(
        id=model_id,
        name=model_id,
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


def prepare(native_id: str, alias: str | None = None, **fields: object) -> dict:
    model = make_model(alias or native_id)
    provider = AnthropicUpstreamProvider(api_key="test")
    provider._native_model_names = {model.id: native_id}
    body = {"model": model.id, "messages": [], "max_tokens": 32000, **fields}
    result = provider.prepare_request_body(json.dumps(body).encode(), model)
    assert result is not None
    return json.loads(result)


ADAPTIVE_ONLY = [
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-haiku-5-5",
    "claude-fable-5",
    "claude-fable-5-1",
]

BUDGET_CAPABLE = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-opus-4-5-20251101",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-20250514",
    "claude-3-5-haiku-20241022",
]


@pytest.mark.parametrize("native_id", ADAPTIVE_ONLY)
def test_budgeted_thinking_becomes_adaptive_on_adaptive_only_models(
    native_id: str,
) -> None:
    body = prepare(
        native_id,
        thinking={"type": "enabled", "budget_tokens": 8192, "display": "summarized"},
    )
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "medium"}


def test_dotted_alias_is_gated_on_the_native_id() -> None:
    body = prepare(
        "claude-sonnet-5-5",
        alias="claude-sonnet-5.5",
        thinking={"type": "enabled", "budget_tokens": 1024},
    )
    assert body["model"] == "claude-sonnet-5-5"
    assert body["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize(
    ("budget", "effort"),
    [
        (1024, "low"),
        (2048, "low"),
        (4096, "low"),
        (8192, "medium"),
        (12288, "medium"),
        (16384, "high"),
        (64000, "high"),
    ],
)
def test_budget_maps_to_effort(budget: int, effort: str) -> None:
    body = prepare(
        "claude-sonnet-5-5", thinking={"type": "enabled", "budget_tokens": budget}
    )
    assert body["output_config"] == {"effort": effort}


def test_explicit_effort_wins_over_budget() -> None:
    body = prepare(
        "claude-sonnet-5-5",
        thinking={"type": "enabled", "budget_tokens": 16384},
        output_config={"effort": "low", "format": {"type": "text"}},
    )
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "low", "format": {"type": "text"}}


def test_generic_effort_wins_over_budget() -> None:
    body = prepare(
        "claude-sonnet-5-5",
        thinking={"type": "enabled", "budget_tokens": 16384},
        reasoning_effort="low",
    )
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "low"}
    assert "reasoning_effort" not in body


def test_missing_budget_leaves_effort_to_the_model() -> None:
    body = prepare("claude-sonnet-5-5", thinking={"type": "enabled"})
    assert body["thinking"] == {"type": "adaptive"}
    assert "output_config" not in body


@pytest.mark.parametrize("native_id", BUDGET_CAPABLE)
def test_budgeted_thinking_is_kept_where_anthropic_accepts_it(native_id: str) -> None:
    thinking = {"type": "enabled", "budget_tokens": 2048}
    body = prepare(native_id, thinking=thinking)
    assert body["thinking"] == thinking
    assert "output_config" not in body


@pytest.mark.parametrize("display", ["summarized", "omitted", "updates"])
def test_display_values_are_untouched(display: str) -> None:
    thinking = {"type": "adaptive", "display": display}
    body = prepare("claude-sonnet-5-5", thinking=thinking)
    assert body["thinking"] == thinking


@pytest.mark.parametrize(
    "thinking", [{"type": "adaptive"}, {"type": "disabled"}, "enabled", None]
)
def test_other_thinking_values_are_untouched(thinking: object) -> None:
    body = prepare("claude-sonnet-5-5", thinking=thinking)
    assert body["thinking"] == thinking
