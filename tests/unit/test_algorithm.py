"""Tests for the model prioritization algorithm."""

import os
from unittest.mock import Mock

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.algorithm import (  # noqa: E402
    calculate_model_cost_score,
    get_provider_penalty,
)
from routstr.payment.models import Architecture, Model, Pricing  # noqa: E402


def create_test_model(
    model_id: str,
    prompt_price: float = 0.001,
    completion_price: float = 0.002,
    request_price: float = 0.0,
) -> Model:
    """Helper to create a test model with given pricing."""
    return Model(
        id=model_id,
        name=f"Test {model_id}",
        created=1234567890,
        description="Test model",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="gpt",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=prompt_price,
            completion=completion_price,
            request=request_price,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0,
        ),
    )


def create_test_provider(name: str, base_url: str = "http://test.com") -> Mock:
    """Helper to create a test provider mock."""
    provider = Mock()
    provider.provider_type = name
    provider.base_url = base_url
    return provider


def test_calculate_model_cost_score_basic() -> None:
    """Test basic cost calculation."""
    model = create_test_model("test-model", prompt_price=0.001, completion_price=0.002)
    cost = calculate_model_cost_score(model)

    # Expected: (1000 tokens * 0.001) + (500 tokens * 0.002) = 0.001 + 0.001 = 0.002
    assert cost == 0.002


def test_calculate_model_cost_score_with_request_fee() -> None:
    """Test cost calculation with request fee."""
    model = create_test_model(
        "test-model",
        prompt_price=0.001,
        completion_price=0.002,
        request_price=0.0005,
    )
    cost = calculate_model_cost_score(model)

    # Expected: 0.001 + 0.001 + 0.0005 = 0.0025
    assert cost == 0.0025


def test_calculate_model_cost_score_expensive_model() -> None:
    """Test cost calculation for expensive model."""
    model = create_test_model(
        "expensive-model", prompt_price=0.03, completion_price=0.06
    )
    cost = calculate_model_cost_score(model)

    # Expected: (1000 * 0.03) + (500 * 0.06) = 0.03 + 0.03 = 0.06
    assert cost == 0.06


def test_get_provider_penalty_regular_provider() -> None:
    """Test penalty for regular provider."""
    provider = create_test_provider("regular-provider", "http://provider.com")
    penalty = get_provider_penalty(provider)
    assert penalty == 1.0


def test_get_provider_penalty_openrouter() -> None:
    """Test penalty for OpenRouter."""
    provider = create_test_provider("openrouter", "https://openrouter.ai/api/v1")
    penalty = get_provider_penalty(provider)
    assert penalty == 1.001

