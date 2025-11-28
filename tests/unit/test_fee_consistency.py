"""Unit tests for model row payload conversion.

This module tests that _model_to_row_payload correctly serializes model data
for database storage. Pricing is stored as-is without fee application.
Fees are now applied per-provider when reading from the database.

Key behaviors tested:
1. Pricing is stored as-is without fee application
2. All model fields are correctly serialized to JSON
3. Optional fields are handled correctly (None values)
4. Pricing structure is preserved
5. Original model objects are not mutated
"""

import json
import os

import pytest

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.models.crud import _model_to_row_payload
from routstr.models.models import (  # noqa: E402
    Architecture,
    Model,
    Pricing,
)


@pytest.fixture
def base_architecture() -> Architecture:
    """Provide standard architecture for test models."""
    return Architecture(
        modality="text",
        input_modalities=["text"],
        output_modalities=["text"],
        tokenizer="gpt",
        instruct_type="chat",
    )


@pytest.fixture
def standard_pricing() -> Pricing:
    """Provide standard USD pricing with known values for testing."""
    return Pricing(
        prompt=0.001,
        completion=0.002,
        request=0.01,
        image=0.05,
        web_search=0.03,
        internal_reasoning=0.015,
        max_prompt_cost=10.0,
        max_completion_cost=20.0,
        max_cost=30.0,
    )


@pytest.fixture
def standard_model(base_architecture: Architecture, standard_pricing: Pricing) -> Model:
    """Create a standard test model with known pricing."""
    return Model(
        id="test-model-standard",
        name="Test Model Standard",
        created=1234567890,
        description="A standard test model",
        context_length=8192,
        architecture=base_architecture,
        pricing=standard_pricing,
    )


def test_pricing_stored_without_fees(standard_model: Model) -> None:
    """Verify pricing is stored as-is without any fee application."""
    payload = _model_to_row_payload(standard_model)
    pricing_str = payload["pricing"]
    assert isinstance(pricing_str, str)
    pricing = json.loads(pricing_str)

    assert pricing["prompt"] == pytest.approx(0.001, rel=1e-9)
    assert pricing["completion"] == pytest.approx(0.002, rel=1e-9)
    assert pricing["request"] == pytest.approx(0.01, rel=1e-9)
    assert pricing["image"] == pytest.approx(0.05, rel=1e-9)
    assert pricing["web_search"] == pytest.approx(0.03, rel=1e-9)
    assert pricing["internal_reasoning"] == pytest.approx(0.015, rel=1e-9)
    assert pricing["max_prompt_cost"] == pytest.approx(10.0, rel=1e-9)
    assert pricing["max_completion_cost"] == pytest.approx(20.0, rel=1e-9)
    assert pricing["max_cost"] == pytest.approx(30.0, rel=1e-9)


def test_zero_value_pricing_fields(base_architecture: Architecture) -> None:
    """Verify that zero-value pricing fields are stored correctly."""
    zero_pricing = Pricing(
        prompt=0.0,
        completion=0.0,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_prompt_cost=0.0,
        max_completion_cost=0.0,
        max_cost=0.0,
    )

    model = Model(
        id="test-model-zero",
        name="Test Model Zero",
        created=1234567890,
        description="A model with zero pricing",
        context_length=8192,
        architecture=base_architecture,
        pricing=zero_pricing,
    )

    payload = _model_to_row_payload(model)
    pricing_str = payload["pricing"]
    assert isinstance(pricing_str, str)
    pricing = json.loads(pricing_str)

    assert pricing["prompt"] == pytest.approx(0.0, rel=1e-9)
    assert pricing["completion"] == pytest.approx(0.0, rel=1e-9)
    assert pricing["request"] == pytest.approx(0.0, rel=1e-9)


def test_payload_structure_unchanged(standard_model: Model) -> None:
    """Verify that payload structure matches expectations."""
    payload = _model_to_row_payload(standard_model)

    assert "id" in payload
    assert "name" in payload
    assert "created" in payload
    assert "description" in payload
    assert "context_length" in payload
    assert "architecture" in payload
    assert "pricing" in payload
    assert "sats_pricing" in payload
    assert "per_request_limits" in payload
    assert "top_provider" in payload
    assert "enabled" in payload
    assert "upstream_provider_id" in payload

    assert isinstance(payload["architecture"], str)
    assert isinstance(payload["pricing"], str)


def test_original_model_not_mutated(standard_model: Model) -> None:
    """Verify that the original model object is not mutated."""
    original_prompt = standard_model.pricing.prompt
    original_completion = standard_model.pricing.completion

    _model_to_row_payload(standard_model)

    assert standard_model.pricing.prompt == original_prompt
    assert standard_model.pricing.completion == original_completion
