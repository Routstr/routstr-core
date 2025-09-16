import os
from unittest.mock import AsyncMock, Mock, patch

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.core.settings import settings  # noqa: E402
from routstr.payment.helpers import get_max_cost_for_model  # noqa: E402


async def test_get_max_cost_for_model_known() -> None:
    # Mock DB session behavior
    mock_session = AsyncMock()
    # available ids
    mock_exec_result = Mock()
    mock_exec_result.all = Mock(return_value=[("gpt-4",)])
    mock_session.exec.return_value = mock_exec_result
    # row with sats_pricing
    row = Mock()
    row.sats_pricing = (
        "{"  # minimal required fields for Pricing model
        '"prompt": 0.0, "completion": 0.0, "request": 0.0, '
        '"image": 0.0, "web_search": 0.0, "internal_reasoning": 0.0, '
        '"max_cost": 500'
        "}"
    )
    mock_session.get.return_value = row

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 0):
            cost = await get_max_cost_for_model("gpt-4", session=mock_session)
            assert cost == 500000  # 500 sats * 1000 = msats


async def test_get_max_cost_for_model_unknown() -> None:
    mock_session = AsyncMock()
    mock_exec_result = Mock()
    mock_exec_result.all = Mock(return_value=[])
    mock_session.exec.return_value = mock_exec_result
    mock_session.get.return_value = None

    with patch.object(settings, "fixed_cost_per_request", 100):
        with patch.object(settings, "tolerance_percentage", 0):
            cost = await get_max_cost_for_model("unknown-model", session=mock_session)
            assert cost == 100000


async def test_get_max_cost_for_model_disabled() -> None:
    with patch.object(settings, "fixed_pricing", True):
        with patch.object(settings, "fixed_cost_per_request", 200):
            with patch.object(settings, "tolerance_percentage", 0):
                cost = await get_max_cost_for_model("any-model", session=None)
                assert cost == 200000


async def test_get_max_cost_for_model_tolerance() -> None:
    mock_session = AsyncMock()
    mock_exec_result = Mock()
    mock_exec_result.all = Mock(return_value=[("gpt-4",)])
    mock_session.exec.return_value = mock_exec_result
    row = Mock()
    row.sats_pricing = (
        "{"  # minimal required fields for Pricing model
        '"prompt": 0.0, "completion": 0.0, "request": 0.0, '
        '"image": 0.0, "web_search": 0.0, "internal_reasoning": 0.0, '
        '"max_cost": 500'
        "}"
    )
    mock_session.get.return_value = row

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 10):
            cost = await get_max_cost_for_model("gpt-4", session=mock_session)
            assert cost == 450000  # 500 sats * 1000 * 0.9 = 450000
