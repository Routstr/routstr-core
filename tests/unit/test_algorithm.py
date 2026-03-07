"""Tests for the model prioritization algorithm."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.algorithm import (  # noqa: E402
    calculate_model_cost_score,
    create_model_mappings,
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


def create_test_provider(
    name: str,
    base_url: str = "http://test.com",
    *,
    db_id: int | None = None,
    models: list[Model] | None = None,
    upstream_name: str | None = None,
) -> Mock:
    """Helper to create a test provider mock."""
    provider = Mock()
    provider.provider_type = name
    provider.base_url = base_url
    provider.db_id = db_id
    provider.upstream_name = upstream_name or name
    provider.get_cached_models.return_value = models or []
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


def test_create_model_mappings_includes_db_override_for_missing_cached_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model overrides should still map when provider discovery misses the model."""
    provider = create_test_provider(
        "azure",
        "https://example.openai.azure.com/openai/v1",
        db_id=7,
        models=[],
    )
    override_model = create_test_model("azure/gpt-4o")
    override_model.canonical_slug = "azure-deployment"

    def fake_row_to_model(*args, **kwargs) -> Model:  # type: ignore[no-untyped-def]
        return override_model

    monkeypatch.setattr("routstr.payment.models._row_to_model", fake_row_to_model)

    override_row = SimpleNamespace(id="azure/gpt-4o", upstream_provider_id=7, enabled=True)

    model_instances, provider_map, unique_models = create_model_mappings(
        upstreams=[provider],
        overrides_by_id={"azure/gpt-4o": (override_row, 1.01)},
        disabled_model_ids=set(),
    )

    assert "azure/gpt-4o" in model_instances
    assert provider_map["azure/gpt-4o"] == [provider]
    assert "gpt-4o" in unique_models


def test_create_model_mappings_dedupes_with_provider_identity_not_provider_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different provider instances of same type should both survive dedupe."""
    provider_a_model = create_test_model(
        "azure/gpt-4o", prompt_price=0.01, completion_price=0.01
    )
    provider_a = create_test_provider(
        "azure",
        "https://a.openai.azure.com/openai/v1",
        db_id=1,
        models=[provider_a_model],
        upstream_name="azure-a",
    )
    provider_b = create_test_provider(
        "azure",
        "https://b.openai.azure.com/openai/v1",
        db_id=2,
        models=[],
        upstream_name="azure-b",
    )

    override_model = create_test_model(
        "azure/gpt-4o", prompt_price=0.001, completion_price=0.001
    )
    override_model.canonical_slug = "azure-b-deployment"

    def fake_row_to_model(*args, **kwargs) -> Model:  # type: ignore[no-untyped-def]
        return override_model

    monkeypatch.setattr("routstr.payment.models._row_to_model", fake_row_to_model)

    override_row = SimpleNamespace(id="azure/gpt-4o", upstream_provider_id=2, enabled=True)

    _, provider_map, _ = create_model_mappings(
        upstreams=[provider_a, provider_b],
        overrides_by_id={"azure/gpt-4o": (override_row, 1.01)},
        disabled_model_ids=set(),
    )

    providers_for_alias = provider_map["azure/gpt-4o"]
    assert provider_a in providers_for_alias
    assert provider_b in providers_for_alias
    assert len(providers_for_alias) == 2
