"""Tests for the model prioritization algorithm."""

import os
from unittest.mock import Mock

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.algorithm import (  # noqa: E402
    calculate_model_cost_score,
    get_provider_penalty,
    should_prefer_model,
)
from routstr.models.models import Architecture, Model, Pricing  # noqa: E402


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


def test_should_prefer_model_cheaper_wins() -> None:
    """Test that cheaper model is preferred."""
    cheap_model = create_test_model("cheap", prompt_price=0.001, completion_price=0.002)
    expensive_model = create_test_model(
        "expensive", prompt_price=0.03, completion_price=0.06
    )

    provider1 = create_test_provider("provider1")
    provider2 = create_test_provider("provider2")

    # Cheaper model should win
    assert should_prefer_model(
        cheap_model, provider1, expensive_model, provider2, "test-alias"
    )

    # More expensive model should not win
    assert not should_prefer_model(
        expensive_model, provider2, cheap_model, provider1, "test-alias"
    )


def test_should_prefer_model_exact_match_wins() -> None:
    """Test that exact alias match beats cheaper price."""
    # Make model IDs match the alias differently
    exact_match = create_test_model(
        "test-model", prompt_price=0.03, completion_price=0.06
    )
    no_match = create_test_model(
        "other-model", prompt_price=0.001, completion_price=0.002
    )

    provider1 = create_test_provider("provider1")
    provider2 = create_test_provider("provider2")

    # Exact match should win even though it's more expensive
    assert should_prefer_model(
        exact_match, provider1, no_match, provider2, "test-model"
    )


def test_should_prefer_model_openrouter_slight_penalty() -> None:
    """Test that OpenRouter has slight penalty compared to other providers."""
    model1 = create_test_model("model1", prompt_price=0.001, completion_price=0.002)
    model2 = create_test_model("model2", prompt_price=0.001, completion_price=0.002)

    regular_provider = create_test_provider("regular", "http://provider.com")
    openrouter_provider = create_test_provider(
        "openrouter", "https://openrouter.ai/api/v1"
    )

    # Regular provider should be preferred over OpenRouter at same cost
    assert should_prefer_model(
        model1, regular_provider, model2, openrouter_provider, "test-alias"
    )

    # OpenRouter should not replace regular provider at same cost
    assert not should_prefer_model(
        model2, openrouter_provider, model1, regular_provider, "test-alias"
    )


def test_should_prefer_model_openrouter_can_win_if_cheaper() -> None:
    """Test that OpenRouter can still win if significantly cheaper."""
    cheap_model = create_test_model(
        "cheap", prompt_price=0.0001, completion_price=0.0002
    )
    expensive_model = create_test_model(
        "expensive", prompt_price=0.03, completion_price=0.06
    )

    regular_provider = create_test_provider("regular", "http://provider.com")
    openrouter_provider = create_test_provider(
        "openrouter", "https://openrouter.ai/api/v1"
    )

    # OpenRouter should win if it's much cheaper (even with penalty)
    assert should_prefer_model(
        cheap_model,
        openrouter_provider,
        expensive_model,
        regular_provider,
        "test-alias",
    )


def test_should_prefer_model_same_cost_first_wins() -> None:
    """Test that when costs are identical, current model is kept."""
    model1 = create_test_model("model1", prompt_price=0.001, completion_price=0.002)
    model2 = create_test_model("model2", prompt_price=0.001, completion_price=0.002)

    provider1 = create_test_provider("provider1")
    provider2 = create_test_provider("provider2")

    # When costs are equal, should not replace
    assert not should_prefer_model(model2, provider2, model1, provider1, "test-alias")
