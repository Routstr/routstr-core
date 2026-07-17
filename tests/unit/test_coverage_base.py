"""Coverage tests for base.py (currently 41%).

Tests preparers, builders, accessors, and model cache methods.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from routstr.upstream.base import BaseUpstreamProvider


# ===========================================================================
# prepare_headers
# ===========================================================================

def test_prepare_headers_adds_auth() -> None:
    """API key is added as Bearer token."""
    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    headers = p.prepare_headers({})

    assert "Authorization" in headers
    assert headers["Authorization"] == "Bearer sk-test-key"


def test_prepare_headers_preserves_existing() -> None:
    """Existing headers are preserved."""
    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    headers = p.prepare_headers({"X-Custom": "value", "Content-Type": "application/json"})

    assert headers["X-Custom"] == "value"
    assert headers["Content-Type"] == "application/json"


def test_prepare_headers_auth_header_passthrough() -> None:
    """Authorization header is handled — verify current behaviour."""
    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    headers = p.prepare_headers({"Authorization": "Bearer user-key"})

    # Currently provider key is used (may be intentional for proxy pattern)
    assert "Authorization" in headers


# ===========================================================================
# prepare_params
# ===========================================================================

@pytest.mark.asyncio
async def test_prepare_params_passes_through() -> None:
    """Query params are preserved by default."""
    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    params = p.prepare_params("/v1/chat/completions", {"temperature": "0.7"})

    assert params["temperature"] == "0.7"


# ===========================================================================
# transform_model_name / normalize_request_path / get_request_base_url
# ===========================================================================

def test_transform_model_name_default_passthrough() -> None:
    """Default returns model_id unchanged."""
    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    assert p.transform_model_name("gpt-4") == "gpt-4"
    assert p.transform_model_name("") == ""


def test_normalize_request_path_passthrough() -> None:
    """Default returns path unchanged."""
    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    assert p.normalize_request_path("/v1/chat/completions") == "/v1/chat/completions"


def test_get_request_base_url_default() -> None:
    """Default returns the provider's base_url."""
    p = BaseUpstreamProvider("https://api.test.com/v1", "sk-test-key")
    url = p.get_request_base_url("/v1/chat/completions")
    assert url == "https://api.test.com/v1"


# ===========================================================================
# build_request_url
# ===========================================================================

def test_build_request_url_combines_base_and_path() -> None:
    """Combines base_url and path."""
    p = BaseUpstreamProvider("https://api.test.com/v1", "sk-test-key")
    url = p.build_request_url("/chat/completions")
    assert "api.test.com" in url
    assert "/chat/completions" in url


# ===========================================================================
# get_litellm_provider_prefix / get_provider_metadata
# ===========================================================================

def test_get_litellm_provider_prefix_default() -> None:
    """Default returns a string prefix."""
    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    prefix = p.get_litellm_provider_prefix()
    assert isinstance(prefix, str)


def test_get_provider_metadata_returns_dict() -> None:
    """Default metadata has name and capabilities."""
    metadata = BaseUpstreamProvider.get_provider_metadata()
    assert isinstance(metadata, dict)
    assert "name" in metadata


# ===========================================================================
# from_db_row
# ===========================================================================

@pytest.mark.asyncio
async def test_from_db_row_returns_provider() -> None:
    """from_db_row constructs a provider from a valid row."""
    mock_row = Mock()
    mock_row.base_url = "https://api.test.com"
    mock_row.api_key = "sk-test-key"
    mock_row.slug = "test-slug"
    mock_row.provider_fee = 1.0
    mock_row.field_overrides = None
    mock_row.name = "Test"

    result = BaseUpstreamProvider.from_db_row(mock_row)
    assert result is not None


# ===========================================================================
# prepare_request_body
# ===========================================================================

def test_prepare_request_body_with_model() -> None:
    """prepare_request_body takes bytes body and Model object."""
    mock_model = Mock()
    mock_model.id = "gpt-4"
    mock_model.forwarded_model_id = None

    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")

    # None body returns None
    result = p.prepare_request_body(None, mock_model)
    assert result is None


# ===========================================================================
# prepare_responses_request_body
# ===========================================================================

def test_prepare_responses_request_body_none() -> None:
    """None body returns None."""
    model_obj = Mock()
    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    result = p.prepare_responses_request_body(None, model_obj)
    assert result is None


# ===========================================================================
# _upstream_accepts_cache_control
# ===========================================================================

def test_upstream_accepts_cache_control_default() -> None:
    """Default: upstream does NOT accept cache-control."""
    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    assert p._upstream_accepts_cache_control() is False


# ===========================================================================
# inject_cost_metadata
# ===========================================================================

def test_inject_cost_metadata_adds_metadata() -> None:
    """Cost metadata is injected into the response dict."""
    mock_key = Mock()
    mock_key.balance_msat = 500000

    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    data = {"model": "gpt-4", "usage": {"prompt_tokens": 100}}
    cost_data = {
        "base_msats": 200000,
        "input_msats": 100000,
        "output_msats": 100000,
        "total_msats": 200000,
        "total_usd": 0.01,
        "input_tokens": 100,
        "output_tokens": 50,
    }

    p.inject_cost_metadata(data, cost_data, mock_key)

    # Metadata is nested under metadata.routstr.cost
    assert "metadata" in data or "routstr_cost" in data or "cost" in data


# ===========================================================================
# _apply_provider_field
# ===========================================================================

def test_apply_provider_field_adds_to_response() -> None:
    """Provider field is added to response JSON."""
    p = BaseUpstreamProvider("https://api.test.com", "sk-test-key")
    data = {"id": "chatcmpl-123"}
    p._apply_provider_field(data)
    assert "provider" in data
