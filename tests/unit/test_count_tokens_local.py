"""Unit tests for the local count_tokens shim.

The shim runs whenever an upstream that does not support Anthropic's
``/v1/messages`` endpoint is asked for a token count. It must always
return a 200 JSON ``{"input_tokens": N}`` response and must never raise.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream import count_tokens as count_tokens_module
from routstr.upstream.count_tokens import count_tokens_locally


def _make_model(model_id: str = "anthropic/claude-3-5-sonnet") -> Model:
    pricing = Pricing(prompt=0.000003, completion=0.000015)
    architecture = Architecture(
        modality="text",
        input_modalities=["text"],
        output_modalities=["text"],
        tokenizer="cl100k_base",
        instruct_type=None,
    )
    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=200_000,
        architecture=architecture,
        pricing=pricing,
    )


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def _read_payload(response: Any) -> dict[str, Any]:
    body = response.body if isinstance(response.body, bytes) else bytes(response.body)
    return json.loads(body.decode())


def test_returns_input_tokens_for_simple_messages() -> None:
    model = _make_model()
    request_body = _body(
        {
            "model": model.id,
            "messages": [{"role": "user", "content": "hello world"}],
        }
    )

    response = count_tokens_locally(request_body, model)

    assert response.status_code == 200
    assert response.media_type == "application/json"
    payload = _read_payload(response)
    assert "input_tokens" in payload
    assert isinstance(payload["input_tokens"], int)
    assert payload["input_tokens"] >= 0


def test_falls_back_to_estimator_when_litellm_raises() -> None:
    model = _make_model()
    request_body = _body(
        {
            "model": model.id,
            "messages": [{"role": "user", "content": "this is a longer message"}],
        }
    )

    with patch.object(
        count_tokens_module,
        "_count_with_litellm",
        side_effect=RuntimeError("boom"),
    ):
        response = count_tokens_locally(request_body, model)

    assert response.status_code == 200
    payload = _read_payload(response)
    assert payload["input_tokens"] >= 1


def test_handles_missing_model_object() -> None:
    request_body = _body(
        {
            "model": "anthropic/claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    response = count_tokens_locally(request_body, None)

    assert response.status_code == 200
    payload = _read_payload(response)
    assert payload["input_tokens"] >= 0


def test_handles_empty_request_body() -> None:
    response = count_tokens_locally(b"", _make_model())

    assert response.status_code == 200
    payload = _read_payload(response)
    assert payload["input_tokens"] >= 0


def test_handles_malformed_json() -> None:
    response = count_tokens_locally(b"not-json", _make_model())

    assert response.status_code == 200
    payload = _read_payload(response)
    assert payload["input_tokens"] >= 0


def test_includes_system_prompt_in_count() -> None:
    model = _make_model()
    short = _body(
        {
            "model": model.id,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    with_system = _body(
        {
            "model": model.id,
            "system": "You are a helpful assistant with a long preamble " * 10,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    short_count = _read_payload(count_tokens_locally(short, model))["input_tokens"]
    long_count = _read_payload(count_tokens_locally(with_system, model))["input_tokens"]

    assert long_count > short_count


def test_supports_anthropic_system_block_list() -> None:
    model = _make_model()
    request_body = _body(
        {
            "model": model.id,
            "system": [{"type": "text", "text": "be terse" * 50}],
            "messages": [{"role": "user", "content": "ok"}],
        }
    )

    response = count_tokens_locally(request_body, model)

    payload = _read_payload(response)
    assert payload["input_tokens"] > 0


def test_uses_forwarded_model_id_when_present() -> None:
    model = _make_model("anthropic/claude-3-5-sonnet")
    model.forwarded_model_id = "claude-3-5-sonnet-20241022"
    request_body = _body(
        {
            "model": "ignored",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    captured: dict[str, Any] = {}

    def _capture(model_name: str, body: dict[str, Any]) -> int:
        captured["model"] = model_name
        return 7

    with patch.object(count_tokens_module, "_count_with_litellm", side_effect=_capture):
        response = count_tokens_locally(request_body, model)

    assert captured["model"] == "claude-3-5-sonnet-20241022"
    assert _read_payload(response)["input_tokens"] == 7
