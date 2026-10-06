"""Billing and routing for System One decision models on upstreams without usage."""

from __future__ import annotations

import json
import os
from typing import Any

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

import pytest  # noqa: E402

from routstr.payment.models import (  # noqa: E402
    Architecture,
    Model,
    Pricing,
    is_decision_model,
)
from routstr.proxy import _route_accepts_model  # noqa: E402
from routstr.upstream.base import _apply_estimated_usage  # noqa: E402
from routstr.upstream.count_tokens import MissingUsageEstimator  # noqa: E402


def _model(output_modalities: list[str], model_id: str = "laya-rl-agent") -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=32_000,
        architecture=Architecture(
            modality=f"text->{'+'.join(output_modalities)}",
            input_modalities=["text"],
            output_modalities=output_modalities,
            tokenizer="Other",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.042 / 1_000_000, completion=0.0),
    )


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


_STATE = "deploy touched the billing module and the migration drops a column " * 40

_SYSTEMONE_BODY = {
    "model": "laya-rl-agent",
    "state": _STATE,
    "questions": {
        "risk": {
            "type": "score",
            "question": "How risky is this change?",
            "levels": ["low", "medium", "high"],
        }
    },
}

_SYSTEMONE_RESPONSE = {
    "model": "laya-rl-agent",
    "answers": {"risk": {"answer": "high", "probabilities": [0.1, 0.2, 0.7]}},
}


def test_is_decision_model() -> None:
    assert is_decision_model(_model(["decisions"]))
    assert not is_decision_model(_model(["text"]))
    assert not is_decision_model(None)


def test_systemone_input_is_counted_from_state_and_questions() -> None:
    usage = MissingUsageEstimator(_body(_SYSTEMONE_BODY), None).response_data()["usage"]
    assert usage["input_tokens"] >= len(_STATE) // 4
    assert usage["output_tokens"] == 0


def test_systemone_response_without_text_still_yields_estimate() -> None:
    estimator = MissingUsageEstimator(_body(_SYSTEMONE_BODY), None)
    estimator.observe(_SYSTEMONE_RESPONSE)

    usage = estimator.estimated_usage("laya-rl-agent")

    assert usage is not None
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] == 0
    assert usage["estimated"] is True


def test_embeddings_response_without_usage_yields_estimate() -> None:
    body = {"model": "nomic-embed-text", "input": ["first doc " * 50, "second"]}
    estimator = MissingUsageEstimator(_body(body), None)
    estimator.observe(
        {
            "object": "list",
            "model": "nomic-embed-text",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        }
    )

    usage = estimator.estimated_usage("nomic-embed-text")

    assert usage is not None
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] == 0


def test_chat_response_without_text_still_refunds() -> None:
    body = {"model": "llama3", "messages": [{"role": "user", "content": "hi"}]}
    estimator = MissingUsageEstimator(_body(body), None)
    estimator.observe({"model": "llama3", "choices": [{"message": {"content": ""}}]})

    assert estimator.estimated_usage("llama3") is None


def test_x_cashu_systemone_response_is_billed_from_estimate() -> None:
    response_json: dict[str, Any] = dict(_SYSTEMONE_RESPONSE)

    _apply_estimated_usage(
        response_json,
        _body(_SYSTEMONE_BODY),
        _model(["decisions"]),
        1000,
        "sat",
        "chat",
    )

    usage = response_json["usage"]
    assert usage["input_tokens"] >= len(_STATE) // 4
    assert usage["output_tokens"] == 0


def test_measured_usage_is_never_replaced() -> None:
    response_json = {**_SYSTEMONE_RESPONSE, "usage": {"input_tokens": 7}}

    _apply_estimated_usage(
        response_json, _body(_SYSTEMONE_BODY), None, 1000, "sat", "chat"
    )

    assert response_json["usage"] == {"input_tokens": 7}


@pytest.mark.parametrize(
    "path",
    [
        "v1/chat/completions",
        "chat/completions",
        "v1/completions",
        "v1/responses",
        "v1/messages",
        "v1/messages/count_tokens",
        "v1/embeddings",
    ],
)
def test_decision_model_is_refused_on_text_routes(path: str) -> None:
    assert not _route_accepts_model(path, _model(["decisions"]))


@pytest.mark.parametrize("path", ["v1/systemone", "systemone", "v1/systemone/"])
def test_decision_model_is_accepted_on_systemone(path: str) -> None:
    assert _route_accepts_model(path, _model(["decisions"]))


@pytest.mark.parametrize(
    "path", ["v1/chat/completions", "v1/responses", "v1/embeddings", "v1/systemone"]
)
def test_unmarked_model_is_accepted_everywhere(path: str) -> None:
    """Providers default every model to ``text``; only an explicit mark gates."""
    assert _route_accepts_model(path, _model(["text"]))
