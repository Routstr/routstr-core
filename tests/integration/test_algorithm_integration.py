"""Integration tests for algorithm create_model_mappings function"""

import os
import pytest
from unittest.mock import Mock, patch

os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.algorithm import create_model_mappings
from routstr.payment.models import Model, Pricing


@pytest.mark.asyncio
async def test_create_model_mappings_basic() -> None:
    """Test basic model mapping creation"""
    from routstr.upstream.openai import OpenAIProvider

    mock_provider = Mock(spec=OpenAIProvider)
    mock_provider.upstream_name = "openai"
    mock_provider.base_url = "https://api.openai.com/v1"
    mock_provider.get_models = Mock(
        return_value=[
            Model(
                id="gpt-4",
                name="GPT-4",
                created=1234567890,
                description="Test model",
                context_length=8192,
                architecture={"modality": "text"},
                pricing=Pricing(
                    prompt=0.001,
                    completion=0.002,
                    request=0.0,
                    image=0.0,
                    web_search=0.0,
                    internal_reasoning=0.0,
                    max_cost=0.0,
                ),
            )
        ]
    )

    upstreams = [mock_provider]
    overrides_by_id = {}
    disabled_model_ids = set()

    model_instances, provider_map, unique_models = create_model_mappings(
        upstreams, overrides_by_id, disabled_model_ids
    )

    assert isinstance(model_instances, dict)
    assert isinstance(provider_map, dict)
    assert isinstance(unique_models, dict)


@pytest.mark.asyncio
async def test_create_model_mappings_with_overrides() -> None:
    """Test model mapping with database overrides"""
    from routstr.core.db import ModelRow, UpstreamProviderRow
    from routstr.upstream.openai import OpenAIProvider

    mock_provider = Mock(spec=OpenAIProvider)
    mock_provider.upstream_name = "openai"
    mock_provider.base_url = "https://api.openai.com/v1"
    mock_provider.get_models = Mock(return_value=[])

    override_row = ModelRow(
        id="gpt-4-override",
        upstream_provider_id=1,
        name="GPT-4 Override",
        description="Override model",
        created=1234567890,
        context_length=8192,
        architecture='{"modality": "text"}',
        pricing='{"prompt": 0.0005, "completion": 0.001, "request": 0.0}',
        enabled=True,
    )

    provider_row = UpstreamProviderRow(
        id=1,
        provider_type="openai",
        base_url="https://api.openai.com/v1",
        api_key="test",
        provider_fee=1.01,
    )

    upstreams = [mock_provider]
    overrides_by_id = {"gpt-4-override": (override_row, provider_row)}
    disabled_model_ids = set()

    model_instances, provider_map, unique_models = create_model_mappings(
        upstreams, overrides_by_id, disabled_model_ids
    )

    assert "gpt-4-override" in model_instances


@pytest.mark.asyncio
async def test_create_model_mappings_with_disabled_models() -> None:
    """Test that disabled models are excluded from mappings"""
    from routstr.upstream.openai import OpenAIProvider

    mock_provider = Mock(spec=OpenAIProvider)
    mock_provider.upstream_name = "openai"
    mock_provider.base_url = "https://api.openai.com/v1"
    mock_provider.get_models = Mock(
        return_value=[
            Model(
                id="gpt-4",
                name="GPT-4",
                created=1234567890,
                description="Test model",
                context_length=8192,
                architecture={"modality": "text"},
                pricing=Pricing(
                    prompt=0.001,
                    completion=0.002,
                    request=0.0,
                    image=0.0,
                    web_search=0.0,
                    internal_reasoning=0.0,
                    max_cost=0.0,
                ),
            )
        ]
    )

    upstreams = [mock_provider]
    overrides_by_id = {}
    disabled_model_ids = {"gpt-4"}

    model_instances, provider_map, unique_models = create_model_mappings(
        upstreams, overrides_by_id, disabled_model_ids
    )

    assert "gpt-4" not in model_instances


@pytest.mark.asyncio
async def test_create_model_mappings_multiple_providers() -> None:
    """Test model mapping with multiple providers offering same model"""
    from routstr.upstream.openai import OpenAIProvider
    from routstr.upstream.openrouter import OpenRouterProvider

    mock_openai = Mock(spec=OpenAIProvider)
    mock_openai.upstream_name = "openai"
    mock_openai.base_url = "https://api.openai.com/v1"
    mock_openai.get_models = Mock(
        return_value=[
            Model(
                id="gpt-4",
                name="GPT-4",
                created=1234567890,
                description="Test model",
                context_length=8192,
                architecture={"modality": "text"},
                pricing=Pricing(
                    prompt=0.001,
                    completion=0.002,
                    request=0.0,
                    image=0.0,
                    web_search=0.0,
                    internal_reasoning=0.0,
                    max_cost=0.0,
                ),
            )
        ]
    )

    mock_openrouter = Mock(spec=OpenRouterProvider)
    mock_openrouter.upstream_name = "openrouter"
    mock_openrouter.base_url = "https://openrouter.ai/api/v1"
    mock_openrouter.get_models = Mock(
        return_value=[
            Model(
                id="openai/gpt-4",
                name="GPT-4",
                created=1234567890,
                description="Test model",
                context_length=8192,
                architecture={"modality": "text"},
                pricing=Pricing(
                    prompt=0.0008,
                    completion=0.0015,
                    request=0.0,
                    image=0.0,
                    web_search=0.0,
                    internal_reasoning=0.0,
                    max_cost=0.0,
                ),
            )
        ]
    )

    upstreams = [mock_openai, mock_openrouter]
    overrides_by_id = {}
    disabled_model_ids = set()

    model_instances, provider_map, unique_models = create_model_mappings(
        upstreams, overrides_by_id, disabled_model_ids
    )

    assert isinstance(model_instances, dict)
    assert isinstance(provider_map, dict)
