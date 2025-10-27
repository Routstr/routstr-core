"""Unit tests for upstream provider fee application to USD pricing.

This module tests the fix for issue #188: "Upstream provider fee not being applied
to USD pricing (only sats)". The fix ensures that both exchange_fee and
upstream_provider_fee are correctly applied to USD pricing when models are stored
in the database via the _model_to_row_payload function.

Key behaviors tested:
1. exchange_fee is applied to all USD pricing fields
2. upstream_provider_fee is applied to all USD pricing fields
3. Both fees are compounded correctly (multiplied together)
4. Fees apply to all pricing attributes (prompt, completion, request, etc.)
5. Fees apply to max cost fields (max_prompt_cost, max_completion_cost, max_cost)
6. sats_pricing remains unaffected (fees not double-applied)
7. Zero-value pricing fields are handled correctly
8. Edge cases and boundary conditions are properly handled
"""

import json
import os
from unittest.mock import patch

import pytest

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.core.settings import settings  # noqa: E402
from routstr.payment.models import (  # noqa: E402
    Architecture,
    Model,
    Pricing,
    _model_to_row_payload,
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
        prompt=0.001,  # $0.001 per prompt token
        completion=0.002,  # $0.002 per completion token
        request=0.01,  # $0.01 per request
        image=0.05,  # $0.05 per image
        web_search=0.03,  # $0.03 per web search
        internal_reasoning=0.015,  # $0.015 per internal reasoning token
        max_prompt_cost=10.0,  # $10 max prompt cost
        max_completion_cost=20.0,  # $20 max completion cost
        max_cost=30.0,  # $30 max total cost
    )


@pytest.fixture
def standard_model(base_architecture: Architecture, standard_pricing: Pricing) -> Model:
    """Create a standard test model with known pricing."""
    return Model(
        id="test-model-standard",
        name="Test Model Standard",
        created=1234567890,
        description="A standard test model for fee application",
        context_length=8192,
        architecture=base_architecture,
        pricing=standard_pricing,
    )


# =============================================================================
# Individual Fee Application Tests
# =============================================================================


def test_exchange_fee_applied_to_usd_pricing(standard_model: Model) -> None:
    """Verify exchange_fee is applied to all USD pricing fields."""
    exchange_fee = 1.005  # 0.5% fee
    upstream_fee = 1.0  # No upstream fee

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(standard_model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # Verify all pricing fields have exchange fee applied
            assert pricing["prompt"] == pytest.approx(0.001 * exchange_fee, rel=1e-9)
            assert pricing["completion"] == pytest.approx(
                0.002 * exchange_fee, rel=1e-9
            )
            assert pricing["request"] == pytest.approx(0.01 * exchange_fee, rel=1e-9)
            assert pricing["image"] == pytest.approx(0.05 * exchange_fee, rel=1e-9)
            assert pricing["web_search"] == pytest.approx(0.03 * exchange_fee, rel=1e-9)
            assert pricing["internal_reasoning"] == pytest.approx(
                0.015 * exchange_fee, rel=1e-9
            )

            # Verify max cost fields have exchange fee applied
            assert pricing["max_prompt_cost"] == pytest.approx(
                10.0 * exchange_fee, rel=1e-9
            )
            assert pricing["max_completion_cost"] == pytest.approx(
                20.0 * exchange_fee, rel=1e-9
            )
            assert pricing["max_cost"] == pytest.approx(30.0 * exchange_fee, rel=1e-9)


def test_upstream_provider_fee_applied_to_usd_pricing(standard_model: Model) -> None:
    """Verify upstream_provider_fee is applied to all USD pricing fields."""
    exchange_fee = 1.0  # No exchange fee
    upstream_fee = 1.05  # 5% upstream provider fee

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(standard_model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # Verify all pricing fields have upstream fee applied
            assert pricing["prompt"] == pytest.approx(0.001 * upstream_fee, rel=1e-9)
            assert pricing["completion"] == pytest.approx(
                0.002 * upstream_fee, rel=1e-9
            )
            assert pricing["request"] == pytest.approx(0.01 * upstream_fee, rel=1e-9)
            assert pricing["image"] == pytest.approx(0.05 * upstream_fee, rel=1e-9)
            assert pricing["web_search"] == pytest.approx(0.03 * upstream_fee, rel=1e-9)
            assert pricing["internal_reasoning"] == pytest.approx(
                0.015 * upstream_fee, rel=1e-9
            )

            # Verify max cost fields have upstream fee applied
            assert pricing["max_prompt_cost"] == pytest.approx(
                10.0 * upstream_fee, rel=1e-9
            )
            assert pricing["max_completion_cost"] == pytest.approx(
                20.0 * upstream_fee, rel=1e-9
            )
            assert pricing["max_cost"] == pytest.approx(30.0 * upstream_fee, rel=1e-9)


# =============================================================================
# Combined Fee Application Tests (Core Issue #188 Fix)
# =============================================================================


def test_both_fees_compounded_correctly(standard_model: Model) -> None:
    """Test that exchange_fee and upstream_provider_fee are compounded (multiplied).

    This is the PRIMARY test for issue #188. Before the fix, only exchange_fee was
    applied to USD pricing. The fix ensures both fees are compounded correctly.
    """
    exchange_fee = 1.005  # 0.5% exchange fee
    upstream_fee = 1.05  # 5% upstream provider fee
    expected_multiplier = exchange_fee * upstream_fee  # 1.05525

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(standard_model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # All pricing fields should be multiplied by the combined fee
            assert pricing["prompt"] == pytest.approx(
                0.001 * expected_multiplier, rel=1e-9
            )
            assert pricing["completion"] == pytest.approx(
                0.002 * expected_multiplier, rel=1e-9
            )
            assert pricing["request"] == pytest.approx(
                0.01 * expected_multiplier, rel=1e-9
            )
            assert pricing["image"] == pytest.approx(
                0.05 * expected_multiplier, rel=1e-9
            )
            assert pricing["web_search"] == pytest.approx(
                0.03 * expected_multiplier, rel=1e-9
            )
            assert pricing["internal_reasoning"] == pytest.approx(
                0.015 * expected_multiplier, rel=1e-9
            )

            # Max cost fields should also have combined fee applied
            assert pricing["max_prompt_cost"] == pytest.approx(
                10.0 * expected_multiplier, rel=1e-9
            )
            assert pricing["max_completion_cost"] == pytest.approx(
                20.0 * expected_multiplier, rel=1e-9
            )
            assert pricing["max_cost"] == pytest.approx(
                30.0 * expected_multiplier, rel=1e-9
            )


def test_default_fee_values_from_settings(standard_model: Model) -> None:
    """Test with actual default fee values from settings.

    Default values per routstr/core/settings.py:
    - exchange_fee: 1.005 (0.5%)
    - upstream_provider_fee: 1.05 (5%)
    Combined: 1.05525 (5.525% total fee)
    """
    # Use actual default values (don't mock)
    payload = _model_to_row_payload(standard_model)
    pricing_str = payload["pricing"]
    assert isinstance(pricing_str, str)
    pricing = json.loads(pricing_str)

    # Calculate expected multiplier with production defaults
    default_exchange_fee = 1.005
    default_upstream_fee = 1.05
    expected_multiplier = default_exchange_fee * default_upstream_fee  # 1.05525

    # Verify pricing is higher than original due to fees
    assert pricing["prompt"] > standard_model.pricing.prompt
    assert pricing["completion"] > standard_model.pricing.completion
    assert pricing["request"] > standard_model.pricing.request

    # Verify exact values with default fees
    assert pricing["prompt"] == pytest.approx(0.001 * expected_multiplier, rel=1e-9)
    assert pricing["completion"] == pytest.approx(0.002 * expected_multiplier, rel=1e-9)
    assert pricing["request"] == pytest.approx(0.01 * expected_multiplier, rel=1e-9)
    assert pricing["image"] == pytest.approx(0.05 * expected_multiplier, rel=1e-9)
    assert pricing["web_search"] == pytest.approx(0.03 * expected_multiplier, rel=1e-9)
    assert pricing["internal_reasoning"] == pytest.approx(
        0.015 * expected_multiplier, rel=1e-9
    )


# =============================================================================
# Varied Fee Scenarios
# =============================================================================


def test_higher_fee_values(standard_model: Model) -> None:
    """Test with significantly higher fee values to ensure scalability."""
    exchange_fee = 1.02  # 2% exchange fee
    upstream_fee = 1.15  # 15% upstream provider fee
    expected_multiplier = exchange_fee * upstream_fee  # 1.173

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(standard_model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            assert pricing["prompt"] == pytest.approx(
                0.001 * expected_multiplier, rel=1e-9
            )
            assert pricing["completion"] == pytest.approx(
                0.002 * expected_multiplier, rel=1e-9
            )
            assert pricing["request"] == pytest.approx(
                0.01 * expected_multiplier, rel=1e-9
            )
            assert pricing["max_cost"] == pytest.approx(
                30.0 * expected_multiplier, rel=1e-9
            )


def test_minimal_fee_values(standard_model: Model) -> None:
    """Test with fees very close to 1.0 (minimal markup)."""
    exchange_fee = 1.001  # 0.1% exchange fee
    upstream_fee = 1.001  # 0.1% upstream provider fee
    expected_multiplier = exchange_fee * upstream_fee  # 1.002001

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(standard_model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # Verify precise calculation even with small fees
            assert pricing["prompt"] == pytest.approx(
                0.001 * expected_multiplier, rel=1e-9
            )
            assert pricing["completion"] == pytest.approx(
                0.002 * expected_multiplier, rel=1e-9
            )


def test_no_fees_applied(standard_model: Model) -> None:
    """Test with both fees set to 1.0 (no markup)."""
    exchange_fee = 1.0  # No fee
    upstream_fee = 1.0  # No fee

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(standard_model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # Prices should remain unchanged
            assert pricing["prompt"] == pytest.approx(0.001, rel=1e-9)
            assert pricing["completion"] == pytest.approx(0.002, rel=1e-9)
            assert pricing["request"] == pytest.approx(0.01, rel=1e-9)
            assert pricing["max_cost"] == pytest.approx(30.0, rel=1e-9)


# =============================================================================
# Zero and Edge Case Pricing Tests
# =============================================================================


def test_zero_value_pricing_fields(base_architecture: Architecture) -> None:
    """Verify that zero-value pricing fields are handled correctly.

    Zero values should remain zero after fee application (0 * multiplier = 0).
    """
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
        id="test-zero-pricing",
        name="Zero Pricing Model",
        created=1234567890,
        description="Model with all zero pricing",
        context_length=8192,
        architecture=base_architecture,
        pricing=zero_pricing,
    )

    exchange_fee = 1.005
    upstream_fee = 1.05

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # All values should remain zero
            assert pricing["prompt"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["completion"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["request"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["image"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["web_search"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["internal_reasoning"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["max_prompt_cost"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["max_completion_cost"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["max_cost"] == pytest.approx(0.0, abs=1e-9)


def test_mixed_zero_and_nonzero_pricing(base_architecture: Architecture) -> None:
    """Test models with some zero and some non-zero pricing fields."""
    mixed_pricing = Pricing(
        prompt=0.001,  # Non-zero
        completion=0.002,  # Non-zero
        request=0.0,  # Zero
        image=0.0,  # Zero
        web_search=0.03,  # Non-zero
        internal_reasoning=0.0,  # Zero
        max_prompt_cost=10.0,  # Non-zero
        max_completion_cost=0.0,  # Zero
        max_cost=15.0,  # Non-zero
    )

    model = Model(
        id="test-mixed-pricing",
        name="Mixed Pricing Model",
        created=1234567890,
        description="Model with mixed zero/non-zero pricing",
        context_length=8192,
        architecture=base_architecture,
        pricing=mixed_pricing,
    )

    exchange_fee = 1.005
    upstream_fee = 1.05
    expected_multiplier = exchange_fee * upstream_fee

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # Non-zero values should have fees applied
            assert pricing["prompt"] == pytest.approx(
                0.001 * expected_multiplier, rel=1e-9
            )
            assert pricing["completion"] == pytest.approx(
                0.002 * expected_multiplier, rel=1e-9
            )
            assert pricing["web_search"] == pytest.approx(
                0.03 * expected_multiplier, rel=1e-9
            )
            assert pricing["max_prompt_cost"] == pytest.approx(
                10.0 * expected_multiplier, rel=1e-9
            )
            assert pricing["max_cost"] == pytest.approx(
                15.0 * expected_multiplier, rel=1e-9
            )

            # Zero values should remain zero
            assert pricing["request"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["image"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["internal_reasoning"] == pytest.approx(0.0, abs=1e-9)
            assert pricing["max_completion_cost"] == pytest.approx(0.0, abs=1e-9)


def test_very_small_pricing_values(base_architecture: Architecture) -> None:
    """Test with very small pricing values to verify precision."""
    tiny_pricing = Pricing(
        prompt=0.000001,  # $0.000001 per token
        completion=0.000002,
        request=0.00001,
        image=0.0001,
        web_search=0.0001,
        internal_reasoning=0.000001,
        max_prompt_cost=0.01,
        max_completion_cost=0.02,
        max_cost=0.03,
    )

    model = Model(
        id="test-tiny-pricing",
        name="Tiny Pricing Model",
        created=1234567890,
        description="Model with very small pricing values",
        context_length=8192,
        architecture=base_architecture,
        pricing=tiny_pricing,
    )

    exchange_fee = 1.005
    upstream_fee = 1.05
    expected_multiplier = exchange_fee * upstream_fee

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # Verify precision is maintained for very small values
            assert pricing["prompt"] == pytest.approx(
                0.000001 * expected_multiplier, rel=1e-6
            )
            assert pricing["completion"] == pytest.approx(
                0.000002 * expected_multiplier, rel=1e-6
            )
            assert pricing["request"] == pytest.approx(
                0.00001 * expected_multiplier, rel=1e-6
            )


def test_very_large_pricing_values(base_architecture: Architecture) -> None:
    """Test with very large pricing values to ensure no overflow."""
    large_pricing = Pricing(
        prompt=100.0,
        completion=200.0,
        request=500.0,
        image=1000.0,
        web_search=750.0,
        internal_reasoning=150.0,
        max_prompt_cost=100000.0,
        max_completion_cost=200000.0,
        max_cost=500000.0,
    )

    model = Model(
        id="test-large-pricing",
        name="Large Pricing Model",
        created=1234567890,
        description="Model with very large pricing values",
        context_length=8192,
        architecture=base_architecture,
        pricing=large_pricing,
    )

    exchange_fee = 1.005
    upstream_fee = 1.05
    expected_multiplier = exchange_fee * upstream_fee

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # Verify large values are handled correctly
            assert pricing["prompt"] == pytest.approx(
                100.0 * expected_multiplier, rel=1e-9
            )
            assert pricing["max_cost"] == pytest.approx(
                500000.0 * expected_multiplier, rel=1e-9
            )


# =============================================================================
# Sats Pricing Isolation Tests
# =============================================================================


def test_sats_pricing_not_modified(
    base_architecture: Architecture, standard_pricing: Pricing
) -> None:
    """Verify that sats_pricing is NOT affected by USD fee application.

    This ensures the fix doesn't break existing sats pricing behavior.
    The fees should only be applied to USD pricing, not sats pricing.
    """
    sats_pricing = Pricing(
        prompt=10.0,
        completion=20.0,
        request=100.0,
        image=500.0,
        web_search=300.0,
        internal_reasoning=150.0,
        max_prompt_cost=10000.0,
        max_completion_cost=20000.0,
        max_cost=30000.0,
    )

    model = Model(
        id="test-with-sats",
        name="Model With Sats Pricing",
        created=1234567890,
        description="Model with both USD and sats pricing",
        context_length=8192,
        architecture=base_architecture,
        pricing=standard_pricing,
        sats_pricing=sats_pricing,
    )

    exchange_fee = 1.005
    upstream_fee = 1.05

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(model)
            sats_pricing_str = payload["sats_pricing"]
            assert isinstance(sats_pricing_str, str)
            sats_pricing_result = json.loads(sats_pricing_str)

            # Sats pricing should be completely unchanged
            assert sats_pricing_result["prompt"] == pytest.approx(10.0, rel=1e-9)
            assert sats_pricing_result["completion"] == pytest.approx(20.0, rel=1e-9)
            assert sats_pricing_result["request"] == pytest.approx(100.0, rel=1e-9)
            assert sats_pricing_result["image"] == pytest.approx(500.0, rel=1e-9)
            assert sats_pricing_result["web_search"] == pytest.approx(300.0, rel=1e-9)
            assert sats_pricing_result["internal_reasoning"] == pytest.approx(
                150.0, rel=1e-9
            )
            assert sats_pricing_result["max_prompt_cost"] == pytest.approx(
                10000.0, rel=1e-9
            )
            assert sats_pricing_result["max_completion_cost"] == pytest.approx(
                20000.0, rel=1e-9
            )
            assert sats_pricing_result["max_cost"] == pytest.approx(30000.0, rel=1e-9)


def test_model_without_sats_pricing(standard_model: Model) -> None:
    """Test models that don't have sats_pricing (None value)."""
    assert standard_model.sats_pricing is None

    exchange_fee = 1.005
    upstream_fee = 1.05

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(standard_model)

            # sats_pricing should remain None
            assert payload["sats_pricing"] is None


# =============================================================================
# Payload Structure Tests
# =============================================================================


def test_payload_structure_unchanged(standard_model: Model) -> None:
    """Verify the database row payload structure is not corrupted by the fix."""
    with patch.object(settings, "exchange_fee", 1.005):
        with patch.object(settings, "upstream_provider_fee", 1.05):
            payload = _model_to_row_payload(standard_model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)

            # Verify all expected keys exist
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

            # Verify types
            assert isinstance(payload["id"], str)
            assert isinstance(payload["name"], str)
            assert isinstance(payload["created"], int)
            assert isinstance(payload["description"], str)
            assert isinstance(payload["context_length"], int)
            assert isinstance(payload["architecture"], str)  # JSON string
            assert isinstance(payload["pricing"], str)  # JSON string
            assert payload["sats_pricing"] is None  # None for this test model

            # Verify JSON fields can be parsed
            architecture_str = payload["architecture"]
            assert isinstance(architecture_str, str)
            architecture = json.loads(architecture_str)
            pricing = json.loads(pricing_str)

            assert isinstance(architecture, dict)
            assert isinstance(pricing, dict)

            # Verify pricing has all expected fields
            expected_pricing_keys = {
                "prompt",
                "completion",
                "request",
                "image",
                "web_search",
                "internal_reasoning",
                "max_prompt_cost",
                "max_completion_cost",
                "max_cost",
            }
            assert set(pricing.keys()) == expected_pricing_keys


def test_all_pricing_fields_present_after_fee_application(
    standard_model: Model,
) -> None:
    """Ensure no pricing fields are accidentally dropped during fee application."""
    with patch.object(settings, "exchange_fee", 1.005):
        with patch.object(settings, "upstream_provider_fee", 1.05):
            payload = _model_to_row_payload(standard_model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # All original pricing fields must be present
            assert "prompt" in pricing
            assert "completion" in pricing
            assert "request" in pricing
            assert "image" in pricing
            assert "web_search" in pricing
            assert "internal_reasoning" in pricing
            assert "max_prompt_cost" in pricing
            assert "max_completion_cost" in pricing
            assert "max_cost" in pricing

            # No extra fields should be added
            assert len(pricing) == 9


# =============================================================================
# Consistency and Regression Tests
# =============================================================================


def test_fee_consistency_across_all_fields(standard_model: Model) -> None:
    """Verify the same fee multiplier is applied consistently to all fields."""
    exchange_fee = 1.005
    upstream_fee = 1.05
    expected_multiplier = exchange_fee * upstream_fee

    with patch.object(settings, "exchange_fee", exchange_fee):
        with patch.object(settings, "upstream_provider_fee", upstream_fee):
            payload = _model_to_row_payload(standard_model)
            pricing_str = payload["pricing"]
            assert isinstance(pricing_str, str)
            pricing = json.loads(pricing_str)

            # Calculate actual multipliers for each field
            prompt_multiplier = pricing["prompt"] / standard_model.pricing.prompt
            completion_multiplier = (
                pricing["completion"] / standard_model.pricing.completion
            )
            request_multiplier = pricing["request"] / standard_model.pricing.request
            image_multiplier = pricing["image"] / standard_model.pricing.image
            web_search_multiplier = (
                pricing["web_search"] / standard_model.pricing.web_search
            )
            internal_reasoning_multiplier = (
                pricing["internal_reasoning"]
                / standard_model.pricing.internal_reasoning
            )
            max_prompt_multiplier = (
                pricing["max_prompt_cost"] / standard_model.pricing.max_prompt_cost
            )
            max_completion_multiplier = (
                pricing["max_completion_cost"]
                / standard_model.pricing.max_completion_cost
            )
            max_cost_multiplier = pricing["max_cost"] / standard_model.pricing.max_cost

            # All multipliers should be identical and equal to expected multiplier
            assert prompt_multiplier == pytest.approx(expected_multiplier, rel=1e-9)
            assert completion_multiplier == pytest.approx(expected_multiplier, rel=1e-9)
            assert request_multiplier == pytest.approx(expected_multiplier, rel=1e-9)
            assert image_multiplier == pytest.approx(expected_multiplier, rel=1e-9)
            assert web_search_multiplier == pytest.approx(expected_multiplier, rel=1e-9)
            assert internal_reasoning_multiplier == pytest.approx(
                expected_multiplier, rel=1e-9
            )
            assert max_prompt_multiplier == pytest.approx(expected_multiplier, rel=1e-9)
            assert max_completion_multiplier == pytest.approx(
                expected_multiplier, rel=1e-9
            )
            assert max_cost_multiplier == pytest.approx(expected_multiplier, rel=1e-9)


def test_multiple_calls_produce_consistent_results(standard_model: Model) -> None:
    """Verify that calling _model_to_row_payload multiple times is idempotent."""
    with patch.object(settings, "exchange_fee", 1.005):
        with patch.object(settings, "upstream_provider_fee", 1.05):
            # Call multiple times
            payload1 = _model_to_row_payload(standard_model)
            payload2 = _model_to_row_payload(standard_model)
            payload3 = _model_to_row_payload(standard_model)

            pricing1_str = payload1["pricing"]
            pricing2_str = payload2["pricing"]
            pricing3_str = payload3["pricing"]
            assert isinstance(pricing1_str, str)
            assert isinstance(pricing2_str, str)
            assert isinstance(pricing3_str, str)
            pricing1 = json.loads(pricing1_str)
            pricing2 = json.loads(pricing2_str)
            pricing3 = json.loads(pricing3_str)

            # All results should be identical
            assert pricing1 == pricing2
            assert pricing2 == pricing3

            # Original model should not be mutated
            assert standard_model.pricing.prompt == 0.001
            assert standard_model.pricing.completion == 0.002


def test_original_model_not_mutated(standard_model: Model) -> None:
    """Ensure the original model object is not modified by fee application."""
    original_prompt = standard_model.pricing.prompt
    original_completion = standard_model.pricing.completion
    original_max_cost = standard_model.pricing.max_cost

    with patch.object(settings, "exchange_fee", 1.005):
        with patch.object(settings, "upstream_provider_fee", 1.05):
            _ = _model_to_row_payload(standard_model)

            # Original model should be unchanged
            assert standard_model.pricing.prompt == original_prompt
            assert standard_model.pricing.completion == original_completion
            assert standard_model.pricing.max_cost == original_max_cost
