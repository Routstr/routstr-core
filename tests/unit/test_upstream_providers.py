"""Unit tests for upstream provider implementations."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_base_provider_prepare_headers_basic() -> None:
    """Test base provider prepare_headers."""
    from routstr.upstream.base import BaseUpstreamProvider

    provider = BaseUpstreamProvider(
        name="test",
        base_url="https://api.test.com",
        api_key="test-key",
        provider_type="generic",
    )

    headers = provider.prepare_headers()
    assert isinstance(headers, dict)
    assert "Authorization" in headers or "api-key" in headers


@pytest.mark.asyncio
async def test_base_provider_prepare_params_basic() -> None:
    """Test base provider prepare_params."""
    from routstr.upstream.base import BaseUpstreamProvider

    provider = BaseUpstreamProvider(
        name="test",
        base_url="https://api.test.com",
        api_key="test-key",
        provider_type="generic",
    )

    params = provider.prepare_params()
    assert isinstance(params, dict)


@pytest.mark.asyncio
async def test_base_provider_apply_provider_fee() -> None:
    """Test base provider fee application."""
    from routstr.upstream.base import BaseUpstreamProvider
    from routstr.payment.models import Pricing

    provider = BaseUpstreamProvider(
        name="test",
        base_url="https://api.test.com",
        api_key="test-key",
        provider_type="generic",
        provider_fee=1.05,
    )

    pricing = Pricing(
        prompt=0.01,
        completion=0.02,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=100.0,
    )

    result = provider._apply_provider_fee_to_model(pricing)
    assert result.prompt == pytest.approx(0.01 * 1.05)
    assert result.completion == pytest.approx(0.02 * 1.05)


@pytest.mark.asyncio
async def test_openai_provider_transform_model_name() -> None:
    """Test OpenAI provider model name transformation."""
    from routstr.upstream.openai import OpenAIProvider

    provider = OpenAIProvider(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        provider_type="openai",
    )

    transformed = provider.transform_model_name("gpt-4")
    assert transformed == "gpt-4"


@pytest.mark.asyncio
async def test_openai_provider_prepare_request_body() -> None:
    """Test OpenAI provider request body preparation."""
    from routstr.upstream.openai import OpenAIProvider

    provider = OpenAIProvider(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        provider_type="openai",
    )

    body = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "Hello"}],
    }

    prepared = provider.prepare_request_body(body)
    assert prepared["model"] == "gpt-4"
    assert "messages" in prepared


@pytest.mark.asyncio
async def test_openai_provider_error_mapping() -> None:
    """Test OpenAI provider error mapping."""
    from routstr.upstream.openai import OpenAIProvider

    provider = OpenAIProvider(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        provider_type="openai",
    )

    error_response = {"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}}
    mapped = provider.map_upstream_error_response(error_response)
    assert mapped is not None


@pytest.mark.asyncio
async def test_anthropic_provider_transform_model_name() -> None:
    """Test Anthropic provider model name transformation."""
    from routstr.upstream.anthropic import AnthropicProvider

    provider = AnthropicProvider(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        provider_type="anthropic",
    )

    transformed = provider.transform_model_name("claude-3-opus")
    assert "claude" in transformed.lower()


@pytest.mark.asyncio
async def test_anthropic_provider_prepare_headers() -> None:
    """Test Anthropic provider header preparation."""
    from routstr.upstream.anthropic import AnthropicProvider

    provider = AnthropicProvider(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        provider_type="anthropic",
    )

    headers = provider.prepare_headers()
    assert "x-api-key" in headers or "anthropic-api-key" in headers.lower()


@pytest.mark.asyncio
async def test_groq_provider_transform_model_name() -> None:
    """Test Groq provider model name transformation."""
    from routstr.upstream.groq import GroqProvider

    provider = GroqProvider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        provider_type="groq",
    )

    transformed = provider.transform_model_name("llama-3")
    assert isinstance(transformed, str)


@pytest.mark.asyncio
async def test_openrouter_provider_penalty() -> None:
    """Test OpenRouter provider penalty application."""
    from routstr.upstream.openrouter import OpenRouterProvider
    from routstr.payment.models import Pricing

    provider = OpenRouterProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        provider_type="openrouter",
        provider_fee=1.001,
    )

    pricing = Pricing(
        prompt=0.01,
        completion=0.02,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=100.0,
    )

    result = provider._apply_provider_fee_to_model(pricing)
    assert result.prompt == pytest.approx(0.01 * 1.001)


@pytest.mark.asyncio
async def test_ollama_provider_prepare_request_body() -> None:
    """Test Ollama provider request body preparation."""
    from routstr.upstream.ollama import OllamaProvider

    provider = OllamaProvider(
        name="ollama",
        base_url="http://localhost:11434",
        api_key="",
        provider_type="ollama",
    )

    body = {
        "model": "llama2",
        "messages": [{"role": "user", "content": "Hello"}],
    }

    prepared = provider.prepare_request_body(body)
    assert "model" in prepared
    assert "prompt" in prepared or "messages" in prepared


@pytest.mark.asyncio
async def test_perplexity_provider_prepare_request_body() -> None:
    """Test Perplexity provider request body preparation."""
    from routstr.upstream.perplexity import PerplexityProvider

    provider = PerplexityProvider(
        name="perplexity",
        base_url="https://api.perplexity.ai",
        api_key="test-key",
        provider_type="perplexity",
    )

    body = {
        "model": "pplx-70b-online",
        "messages": [{"role": "user", "content": "Hello"}],
    }

    prepared = provider.prepare_request_body(body)
    assert "model" in prepared
    assert "messages" in prepared


@pytest.mark.asyncio
async def test_xai_provider_prepare_headers() -> None:
    """Test xAI provider header preparation."""
    from routstr.upstream.xai import XAIProvider

    provider = XAIProvider(
        name="xai",
        base_url="https://api.x.ai/v1",
        api_key="test-key",
        provider_type="xai",
    )

    headers = provider.prepare_headers()
    assert isinstance(headers, dict)


@pytest.mark.asyncio
async def test_azure_provider_prepare_headers() -> None:
    """Test Azure provider header preparation."""
    from routstr.upstream.azure import AzureProvider

    provider = AzureProvider(
        name="azure",
        base_url="https://test.openai.azure.com",
        api_key="test-key",
        provider_type="azure",
    )

    headers = provider.prepare_headers()
    assert isinstance(headers, dict)


@pytest.mark.asyncio
async def test_fireworks_provider_transform_model_name() -> None:
    """Test Fireworks provider model name transformation."""
    from routstr.upstream.fireworks import FireworksProvider

    provider = FireworksProvider(
        name="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key="test-key",
        provider_type="fireworks",
    )

    transformed = provider.transform_model_name("accounts/fireworks/models/llama-v2-7b-chat")
    assert isinstance(transformed, str)
