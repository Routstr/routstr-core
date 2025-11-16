"""Integration tests for algorithm functionality."""

import pytest
from unittest.mock import patch

from routstr.algorithm import create_model_mappings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_model_mappings_basic() -> None:
    """Test create_model_mappings with basic scenario."""
    from routstr.payment.models import Model
    
    mock_models = [
        Model(
            id="gpt-4",
            name="GPT-4",
            sats_pricing=None,
            enabled=True,
        ),
        Model(
            id="gpt-3.5-turbo",
            name="GPT-3.5 Turbo",
            sats_pricing=None,
            enabled=True,
        ),
    ]
    
    with patch("routstr.proxy.get_upstreams", return_value=[]):
        mappings = create_model_mappings(mock_models)
        
        assert isinstance(mappings, dict)
        assert "gpt-4" in mappings
        assert "gpt-3.5-turbo" in mappings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_model_mappings_with_aliases() -> None:
    """Test create_model_mappings with model aliases."""
    from routstr.payment.models import Model
    
    mock_models = [
        Model(
            id="gpt-4",
            name="GPT-4",
            sats_pricing=None,
            enabled=True,
            alias_ids=["gpt-4-turbo", "gpt4"],
        ),
    ]
    
    with patch("routstr.proxy.get_upstreams", return_value=[]):
        mappings = create_model_mappings(mock_models)
        
        assert isinstance(mappings, dict)
        assert "gpt-4" in mappings
        assert "gpt-4-turbo" in mappings
        assert "gpt4" in mappings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_model_mappings_disabled_models() -> None:
    """Test create_model_mappings excludes disabled models."""
    from routstr.payment.models import Model
    
    mock_models = [
        Model(
            id="gpt-4",
            name="GPT-4",
            sats_pricing=None,
            enabled=True,
        ),
        Model(
            id="gpt-3.5-turbo",
            name="GPT-3.5 Turbo",
            sats_pricing=None,
            enabled=False,
        ),
    ]
    
    with patch("routstr.proxy.get_upstreams", return_value=[]):
        mappings = create_model_mappings(mock_models)
        
        assert "gpt-4" in mappings
        assert "gpt-3.5-turbo" not in mappings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_model_mappings_mixed_providers() -> None:
    """Test create_model_mappings with models from different providers."""
    from routstr.payment.models import Model
    
    mock_models = [
        Model(
            id="gpt-4",
            name="GPT-4",
            sats_pricing=None,
            enabled=True,
            upstream_provider_id=1,
        ),
        Model(
            id="claude-3-opus",
            name="Claude 3 Opus",
            sats_pricing=None,
            enabled=True,
            upstream_provider_id=2,
        ),
    ]
    
    with patch("routstr.proxy.get_upstreams", return_value=[]):
        mappings = create_model_mappings(mock_models)
        
        assert isinstance(mappings, dict)
        assert "gpt-4" in mappings
        assert "claude-3-opus" in mappings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_model_mappings_canonical_slug() -> None:
    """Test create_model_mappings with canonical_slug."""
    from routstr.payment.models import Model
    
    mock_models = [
        Model(
            id="gpt-4",
            name="GPT-4",
            sats_pricing=None,
            enabled=True,
            canonical_slug="gpt-4",
        ),
    ]
    
    with patch("routstr.proxy.get_upstreams", return_value=[]):
        mappings = create_model_mappings(mock_models)
        
        assert isinstance(mappings, dict)
        assert "gpt-4" in mappings
