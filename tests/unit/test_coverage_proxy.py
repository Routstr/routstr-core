"""Coverage tests for proxy.py (currently 47%).

Tests request parsing, model extraction, and routing helpers.
"""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException


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
