import os
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.core.settings import settings  # noqa: E402
from routstr.payment.helpers import (  # noqa: E402
    calculate_discounted_max_cost,
    check_token_balance,
    get_max_cost_for_model,
)
from routstr.payment.models import Pricing  # noqa: E402


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


async def test_calculate_discounted_max_cost_no_session() -> None:
    with patch.object(settings, "fixed_pricing", False):
        result = await calculate_discounted_max_cost(123, {}, session=None)
        assert result == 123


async def test_calculate_discounted_max_cost_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_session = AsyncMock()
    mock_exec_result = Mock()
    mock_exec_result.all = Mock(return_value=[("gpt-4",)])
    mock_session.exec.return_value = mock_exec_result

    pricing = {
        "prompt": 0.0,
        "completion": 0.0,
        "request": 0.0,
        "image": 0.0,
        "web_search": 0.0,
        "internal_reasoning": 0.0,
        "max_prompt_cost": 100.0,
        "max_completion_cost": 200.0,
        "max_cost": 300.0,
    }

    async def mock_get_model_cost_info(*args, **kwargs):  # type: ignore
        return Pricing(**pricing)

    monkeypatch.setattr(
        "routstr.payment.helpers.get_model_cost_info", mock_get_model_cost_info
    )

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 0):
            result = await calculate_discounted_max_cost(
                320000,
                {"model": "gpt-4", "max_tokens": 10, "messages": ["one"]},
                session=mock_session,
            )
            assert 0 <= result <= 320000


async def test_check_token_balance_with_fee_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Token:
        unit = "msat"
        amount = 320400

    def mock_deserialize(_value: str) -> Token:
        return Token()

    monkeypatch.setattr(
        "routstr.payment.helpers.deserialize_token_from_string", mock_deserialize
    )
    with patch.object(settings, "cashu_mint_fee_msat", 100):
        headers = {"x-cashu": "token"}
        body = {"model": "gpt-4"}
        check_token_balance(headers, body, max_cost_for_model=320450)


def test_check_token_balance_insufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    class Token:
        unit = "msat"
        amount = 320200

    def mock_deserialize(_value: str) -> Token:
        return Token()

    monkeypatch.setattr(
        "routstr.payment.helpers.deserialize_token_from_string", mock_deserialize
    )
    with patch.object(settings, "cashu_mint_fee_msat", 100):
        headers = {"x-cashu": "token"}
        body = {"model": "gpt-4"}
        with pytest.raises(HTTPException) as exc:
            check_token_balance(headers, body, max_cost_for_model=320450)
        assert exc.value.status_code == 413
        detail = exc.value.detail  # type: ignore[assignment]
        assert isinstance(detail, dict)
        assert detail.get("type") == "minimum_balance_required"
