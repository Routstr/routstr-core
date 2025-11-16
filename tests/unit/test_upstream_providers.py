"""Unit tests for upstream provider implementations"""

import os
from unittest.mock import AsyncMock, Mock, patch

os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

import pytest
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.openai import OpenAIProvider
from routstr.upstream.anthropic import AnthropicProvider


class TestBaseUpstreamProvider:
    """Test base upstream provider functionality"""

    def test_prepare_headers_basic(self) -> None:
        """Test basic header preparation"""
        provider = BaseUpstreamProvider(
            base_url="https://api.example.com",
            api_key="test-key",
            provider_fee=1.01,
        )

        headers = provider.prepare_headers()
        assert isinstance(headers, dict)
        assert "Authorization" in headers or "x-api-key" in headers


    def test_prepare_params_basic(self) -> None:
        """Test basic parameter preparation"""
        provider = BaseUpstreamProvider(
            base_url="https://api.example.com",
            api_key="test-key",
            provider_fee=1.01,
        )

        params = provider.prepare_params()
        assert isinstance(params, dict)


    def test_apply_provider_fee(self) -> None:
        """Test provider fee application"""
        from routstr.payment.models import Pricing

        provider = BaseUpstreamProvider(
            base_url="https://api.example.com",
            api_key="test-key",
            provider_fee=1.05,
        )

        pricing = Pricing(
            prompt=0.001,
            completion=0.002,
            request=0.0,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0,
            max_cost=0.0,
        )

        adjusted = provider._apply_provider_fee_to_model(pricing)

        assert adjusted.prompt == pytest.approx(0.001 * 1.05)
        assert adjusted.completion == pytest.approx(0.002 * 1.05)


class TestOpenAIProvider:
    """Test OpenAI provider implementation"""

    def test_transform_model_name(self) -> None:
        """Test OpenAI model name transformation"""
        provider = OpenAIProvider(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            provider_fee=1.01,
        )

        assert provider.transform_model_name("gpt-4") == "gpt-4"
        assert provider.transform_model_name("gpt-3.5-turbo") == "gpt-3.5-turbo"


    @pytest.mark.asyncio
    async def test_fetch_models(self) -> None:
        """Test fetching models from OpenAI"""
        provider = OpenAIProvider(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            provider_fee=1.01,
        )

        with patch("routstr.upstream.openai.httpx.AsyncClient") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json = Mock(
                return_value={
                    "data": [
                        {
                            "id": "gpt-4",
                            "created": 1234567890,
                            "object": "model",
                        }
                    ]
                }
            )
            mock_client.return_value.__aenter__.return_value.get.return_value = (
                mock_response
            )

            models = await provider.fetch_models()

            assert isinstance(models, list)


    def test_prepare_request_body(self) -> None:
        """Test OpenAI request body preparation"""
        provider = OpenAIProvider(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            provider_fee=1.01,
        )

        body = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hello"}],
        }

        prepared = provider.prepare_request_body(body)

        assert prepared["model"] == "gpt-4"
        assert "messages" in prepared


    def test_error_mapping(self) -> None:
        """Test OpenAI error response mapping"""
        provider = OpenAIProvider(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            provider_fee=1.01,
        )

        error_response = {
            "error": {
                "message": "Rate limit exceeded",
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
            }
        }

        mapped = provider.map_upstream_error_response(error_response)

        assert mapped is not None
        assert "message" in mapped


class TestAnthropicProvider:
    """Test Anthropic provider implementation"""

    def test_transform_model_name(self) -> None:
        """Test Anthropic model name transformation"""
        provider = AnthropicProvider(
            base_url="https://api.anthropic.com",
            api_key="test-key",
            provider_fee=1.01,
        )

        assert provider.transform_model_name("claude-3-opus") == "claude-3-opus-20240229"


    def test_prepare_headers(self) -> None:
        """Test Anthropic header preparation"""
        provider = AnthropicProvider(
            base_url="https://api.anthropic.com",
            api_key="test-key",
            provider_fee=1.01,
        )

        headers = provider.prepare_headers()

        assert "x-api-key" in headers or "anthropic-version" in headers


    @pytest.mark.asyncio
    async def test_fetch_models(self) -> None:
        """Test fetching models from Anthropic"""
        provider = AnthropicProvider(
            base_url="https://api.anthropic.com",
            api_key="test-key",
            provider_fee=1.01,
        )

        with patch("routstr.upstream.anthropic.httpx.AsyncClient") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json = Mock(
                return_value={
                    "data": [
                        {
                            "id": "claude-3-opus-20240229",
                            "created": 1234567890,
                        }
                    ]
                }
            )
            mock_client.return_value.__aenter__.return_value.get.return_value = (
                mock_response
            )

            models = await provider.fetch_models()

            assert isinstance(models, list)


    def test_error_mapping(self) -> None:
        """Test Anthropic error response mapping"""
        provider = AnthropicProvider(
            base_url="https://api.anthropic.com",
            api_key="test-key",
            provider_fee=1.01,
        )

        error_response = {
            "error": {
                "message": "Invalid API key",
                "type": "authentication_error",
            }
        }

        mapped = provider.map_upstream_error_response(error_response)

        assert mapped is not None
