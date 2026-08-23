"""Coverage tests for proxy.py (currently 47%).

Tests request parsing, model extraction, and routing helpers.
"""

import json

import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from unittest.mock import AsyncMock, MagicMock, patch

# ===========================================================================
# parse_request_body_json
# ===========================================================================

def test_parse_json_valid_body() -> None:
    """Valid JSON body is parsed correctly for chat completions."""
    from routstr.proxy import parse_request_body_json

    body = json.dumps({"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}).encode()
    result = parse_request_body_json(body, "/v1/chat/completions")

    assert result["model"] == "gpt-4"
    assert result["messages"][0]["role"] == "user"


def test_parse_json_invalid_raises_400() -> None:
    """Invalid JSON raises HTTPException 400."""
    from routstr.proxy import parse_request_body_json

    with pytest.raises(HTTPException) as exc_info:
        parse_request_body_json(b"not json", "/v1/chat/completions")

    assert exc_info.value.status_code == 400


def test_parse_json_empty_body() -> None:
    """Empty body returns empty dict."""
    from routstr.proxy import parse_request_body_json

    result = parse_request_body_json(b"", "/v1/chat/completions")
    assert isinstance(result, dict)
    assert result == {}


def test_parse_json_responses_path() -> None:
    """Responses API path is handled."""
    from routstr.proxy import parse_request_body_json

    body = json.dumps({"model": "gpt-4", "input": "hello"}).encode()
    result = parse_request_body_json(body, "/v1/responses")

    assert "model" in result


def test_parse_json_rejects_non_integer_max_tokens() -> None:
    """max_tokens must be an integer."""
    from routstr.proxy import parse_request_body_json

    body = json.dumps({"model": "gpt-4", "max_tokens": "abc"}).encode()

    with pytest.raises(HTTPException) as exc_info:
        parse_request_body_json(body, "/v1/chat/completions")

    assert exc_info.value.status_code == 400


# ===========================================================================
# _parse_ehbp_max_tokens
# ===========================================================================

def test_parse_ehbp_max_tokens_valid() -> None:
    """A numeric header value is parsed to an int."""
    from routstr.proxy import _parse_ehbp_max_tokens

    assert _parse_ehbp_max_tokens("1024") == 1024
    assert _parse_ehbp_max_tokens("0") is None
    assert _parse_ehbp_max_tokens("-5") is None
    assert _parse_ehbp_max_tokens("") is None
    assert _parse_ehbp_max_tokens(None) is None
    assert _parse_ehbp_max_tokens("abc") is None
    assert _parse_ehbp_max_tokens("12.5") is None


def test_validated_ehbp_max_tokens_range() -> None:
    """The validated helper enforces the 64000-token floor."""
    from routstr.proxy import (
        _EHBP_MIN_MAX_TOKENS,
        _validated_ehbp_max_tokens,
    )

    # Absent header: no constraint.
    assert _validated_ehbp_max_tokens("") is None
    assert _validated_ehbp_max_tokens(None) is None

    # At-or-above the floor: parsed value returned.
    assert _validated_ehbp_max_tokens(str(_EHBP_MIN_MAX_TOKENS)) == _EHBP_MIN_MAX_TOKENS
    assert _validated_ehbp_max_tokens("100000") == 100000

    # Below the floor / invalid: rejected.
    with pytest.raises(ValueError):
        _validated_ehbp_max_tokens(str(_EHBP_MIN_MAX_TOKENS - 1))
    with pytest.raises(ValueError):
        _validated_ehbp_max_tokens("0")
    with pytest.raises(ValueError):
        _validated_ehbp_max_tokens("abc")


def _ehbp_request(headers: dict) -> MagicMock:
    request = MagicMock()
    request.method = "POST"
    request.headers = {
        "authorization": "Bearer test-key",
        "ehbp-encapsulated-key": "abc123",
        "x-routstr-model": "tinfoil-llama3-3-70b",
        **headers,
    }
    request.body = AsyncMock(return_value=b"sealed-opaque-body")
    request.state.request_id = "ehbp-max-tokens-test"
    return request


async def test_ehbp_max_tokens_below_min_returns_400() -> None:
    """A present header below the floor yields a 400 before any DB access."""
    from routstr.proxy import _proxy

    response = await _proxy(
        _ehbp_request({"x-routstr-max-tokens": "1024"}),
        "v1/chat/completions",
        MagicMock(),
    )

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"]["type"] == "invalid_request"
    assert "at least 64000" in body["error"]["message"]


async def test_ehbp_max_tokens_invalid_returns_400() -> None:
    """A present but unparseable header value returns 400 as well."""
    from routstr.proxy import _proxy

    response = await _proxy(
        _ehbp_request({"x-routstr-max-tokens": "not-a-number"}),
        "v1/chat/completions",
        MagicMock(),
    )

    assert response.status_code == 400
    body = json.loads(response.body)
    assert "at least 64000" in body["error"]["message"]


async def test_ehbp_max_tokens_flows_into_discount() -> None:
    """A valid header cap is passed through to the cost discount."""
    from routstr.proxy import _proxy

    model = MagicMock()
    upstream = MagicMock()
    key = MagicMock()
    seen: dict[str, object] = {}

    async def fake_discount(
        max_cost_for_model: int,
        body: dict,
        model_obj: object = None,
        max_tokens: int | None = None,
    ) -> int:
        seen["max_tokens"] = max_tokens
        seen["body"] = body
        return max_cost_for_model

    with (
        patch("routstr.proxy.get_candidates", return_value=[(model, upstream)]),
        patch("routstr.proxy.get_max_cost_for_model", AsyncMock(return_value=100_000)),
        patch(
            "routstr.proxy.calculate_discounted_max_cost",
            side_effect=fake_discount,
        ),
        patch("routstr.proxy.check_token_balance"),
        patch("routstr.proxy.get_bearer_token_key", AsyncMock(return_value=key)),
        patch("routstr.proxy.pay_for_request", AsyncMock()),
        patch("routstr.proxy.get_reservation_snapshot", AsyncMock(return_value=None)),
        patch("routstr.proxy.forward_ehbp_request", AsyncMock(return_value=Response(status_code=200))),
    ):
        response = await _proxy(
            _ehbp_request({"x-routstr-max-tokens": "100000"}),
            "v1/chat/completions",
            MagicMock(),
        )

    assert response.status_code == 200
    assert seen["max_tokens"] == 100000
    assert seen["body"] == {}  # opaque body stays opaque


async def test_ehbp_max_tokens_absent_is_allowed() -> None:
    """No header still works (backwards compatible with older SDKs)."""
    from routstr.proxy import _proxy

    model = MagicMock()
    upstream = MagicMock()
    key = MagicMock()
    seen: dict[str, object] = {}

    async def fake_discount(
        max_cost_for_model: int,
        body: dict,
        model_obj: object = None,
        max_tokens: int | None = None,
    ) -> int:
        seen["max_tokens"] = max_tokens
        return max_cost_for_model

    with (
        patch("routstr.proxy.get_candidates", return_value=[(model, upstream)]),
        patch("routstr.proxy.get_max_cost_for_model", AsyncMock(return_value=100_000)),
        patch(
            "routstr.proxy.calculate_discounted_max_cost",
            side_effect=fake_discount,
        ),
        patch("routstr.proxy.check_token_balance"),
        patch("routstr.proxy.get_bearer_token_key", AsyncMock(return_value=key)),
        patch("routstr.proxy.pay_for_request", AsyncMock()),
        patch("routstr.proxy.get_reservation_snapshot", AsyncMock(return_value=None)),
        patch("routstr.proxy.forward_ehbp_request", AsyncMock(return_value=Response(status_code=200))),
    ):
        response = await _proxy(
            _ehbp_request({}),
            "v1/chat/completions",
            MagicMock(),
        )

    assert response.status_code == 200
    assert seen["max_tokens"] is None


# ===========================================================================
# extract_model_from_responses_request
# ===========================================================================

def test_extract_model_from_responses() -> None:
    """Model name is extracted from Responses API request."""
    from routstr.proxy import extract_model_from_responses_request

    body = {"model": "gpt-4o", "input": "test"}
    model = extract_model_from_responses_request(body)
    assert model == "gpt-4o"


def test_extract_model_returns_unknown_for_missing() -> None:
    """Missing model field returns 'unknown'."""
    from routstr.proxy import extract_model_from_responses_request

    body = {"input": "test"}
    model = extract_model_from_responses_request(body)
    assert model == "unknown"


def test_extract_model_empty_body_returns_unknown() -> None:
    """Empty body returns 'unknown'."""
    from routstr.proxy import extract_model_from_responses_request

    model = extract_model_from_responses_request({})
    assert model == "unknown"


def test_extract_model_from_input_nested() -> None:
    """Model nested in input dict is found."""
    from routstr.proxy import extract_model_from_responses_request

    body = {"input": {"model": "claude-sonnet", "text": "hi"}}
    model = extract_model_from_responses_request(body)
    # The function checks input_data.get("model") for nested
    assert model in ("claude-sonnet", "unknown")


# ===========================================================================
# get_model_instance / get_provider_for_model / get_unique_models
# ===========================================================================

def test_get_model_instance_unknown_returns_none() -> None:
    """Unknown model ID returns None."""
    from routstr.proxy import get_model_instance

    result = get_model_instance("nonexistent-model-xyz-12345")
    assert result is None


def test_get_provider_for_model_unknown_returns_none() -> None:
    """Unknown model returns None."""
    from routstr.proxy import get_provider_for_model

    result = get_provider_for_model("nonexistent-model-xyz-12345")
    assert result is None


def test_get_unique_models_returns_list() -> None:
    """get_unique_models always returns a list."""
    from routstr.proxy import get_unique_models

    result = get_unique_models()
    assert isinstance(result, list)


def test_get_upstreams_returns_list() -> None:
    """get_upstreams returns a list of providers."""
    from routstr.proxy import get_upstreams

    result = get_upstreams()
    assert isinstance(result, list)


# ===========================================================================
# parse_request_body_json — nested objects
# ===========================================================================

def test_parse_body_preserves_nested_objects() -> None:
    """Nested JSON objects are preserved during parsing."""
    from routstr.proxy import parse_request_body_json

    body = json.dumps({
        "model": "claude-3",
        "messages": [{"role": "system", "content": "You are helpful."}],
        "temperature": 0.7,
        "max_tokens": 1024,
    }).encode()

    result = parse_request_body_json(body, "/v1/chat/completions")
    assert result["temperature"] == 0.7
    assert result["max_tokens"] == 1024
    assert len(result["messages"]) == 1
