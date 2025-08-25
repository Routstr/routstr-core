import os
from unittest.mock import Mock, patch

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.payment.helpers import get_max_cost_for_model  # noqa: E402


def test_get_max_cost_for_model_known() -> None:
    mock_model = Mock()
    mock_model.id = "gpt-4"
    mock_model.sats_pricing = Mock()
    mock_model.sats_pricing.max_cost = 500

    with patch("routstr.payment.helpers.MODELS", [mock_model]):
        with patch("routstr.payment.helpers.MODEL_BASED_PRICING", True):
            cost = get_max_cost_for_model("gpt-4", tolerance_percentage=0)
            assert cost == 500000  # 500 sats * 1000 = msats


def test_get_max_cost_for_model_unknown() -> None:
    with patch("routstr.payment.helpers.MODELS", []):
        with patch("routstr.payment.helpers.COST_PER_REQUEST", 100):
            cost = get_max_cost_for_model("unknown-model", tolerance_percentage=0)
            assert cost == 100


def test_get_max_cost_for_model_disabled() -> None:
    with patch("routstr.payment.helpers.MODEL_BASED_PRICING", False):
        with patch("routstr.payment.helpers.COST_PER_REQUEST", 200):
            cost = get_max_cost_for_model("any-model", tolerance_percentage=0)
            assert cost == 200


def test_get_max_cost_for_model_tolerance() -> None:
    mock_model = Mock()
    mock_model.id = "gpt-4"
    mock_model.sats_pricing = Mock()
    mock_model.sats_pricing.max_cost = 500

    with patch("routstr.payment.helpers.MODELS", [mock_model]):
        with patch("routstr.payment.helpers.MODEL_BASED_PRICING", True):
            cost = get_max_cost_for_model("gpt-4", tolerance_percentage=10)
            assert cost == 450000  # 500 sats * 1000 * 0.9 = 450000
