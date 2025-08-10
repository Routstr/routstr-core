import os
from unittest.mock import MagicMock, patch

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from router.payment.helpers import get_max_cost_for_model  # noqa: E402


def test_get_max_cost_for_model() -> None:
    # Create a mock model with pricing
    mock_model = MagicMock()
    mock_model.id = "gpt-4"
    mock_model.sats_pricing = MagicMock()
    mock_model.sats_pricing.max_cost = 500

    with patch("router.payment.helpers.MODELS", [mock_model]):
        with patch("router.payment.helpers.MODEL_BASED_PRICING", True):
            # Pass the mock model object, not just the ID
            cost = get_max_cost_for_model(mock_model)
            assert cost == 500000  # 500 sats * 1000 = msats


def test_get_max_cost_for_model_unknown() -> None:
    # Create a mock model that won't be in the MODELS list
    unknown_model = MagicMock()
    unknown_model.id = "unknown-model"
    
    with patch("router.payment.helpers.MODELS", []):
        with patch("router.payment.helpers.COST_PER_REQUEST", 100):
            cost = get_max_cost_for_model(unknown_model)
            assert cost == 100


def test_get_max_cost_for_model_disabled() -> None:
    # Create a mock model
    any_model = MagicMock()
    any_model.id = "any-model"
    
    with patch("router.payment.helpers.MODEL_BASED_PRICING", False):
        with patch("router.payment.helpers.COST_PER_REQUEST", 200):
            cost = get_max_cost_for_model(any_model)
            assert cost == 200
