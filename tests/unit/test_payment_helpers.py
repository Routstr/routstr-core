import os
from unittest.mock import AsyncMock, Mock, patch

import pytest

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from router.payment.helpers import get_max_cost_for_model  # noqa: E402


@pytest.mark.asyncio
async def test_get_max_cost_for_model_known() -> None:
    mock_model = Mock()
    mock_model.id = "gpt-4"
    mock_model.sats_pricing = Mock()
    mock_model.sats_pricing.max_cost = 500

    with patch("router.payment.helpers.MODELS", [mock_model]):
        with patch("router.payment.helpers._is_model_based_pricing", AsyncMock(return_value=True)):
            cost = await get_max_cost_for_model("gpt-4")
            assert cost == 500000  # 500 sats * 1000 = msats


@pytest.mark.asyncio
async def test_get_max_cost_for_model_unknown() -> None:
    with patch("router.payment.helpers.MODELS", []):
        with patch("router.payment.helpers._is_model_based_pricing", AsyncMock(return_value=True)):
            with patch("router.payment.helpers._get_cost_per_request", AsyncMock(return_value=100)):
                cost = await get_max_cost_for_model("unknown-model")
                assert cost == 100


@pytest.mark.asyncio
async def test_get_max_cost_for_model_disabled() -> None:
    with patch("router.payment.helpers._is_model_based_pricing", AsyncMock(return_value=False)):
        with patch("router.payment.helpers._get_cost_per_request", AsyncMock(return_value=200)):
            cost = await get_max_cost_for_model("any-model")
            assert cost == 200
