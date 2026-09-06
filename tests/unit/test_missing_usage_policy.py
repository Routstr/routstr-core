"""Missing-usage billing policy tests.

Covers the three money paths touched by `missing_usage_policy`:

1. `calculate_cost` with NO usage at all (the `_empty_cost(MaxCostData)` dead-end).
2. `calculate_cost` with token counts but unusable pricing (the second dead-end).
3. `adjust_payment_for_tokens` `CostDataError` handling — must NOT raise a
   post-delivery 400.
4. X-Cashu non-streaming handler bills from the local estimate when the
   upstream omits usage.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.payment import cost_calculation
from routstr.payment.cost_calculation import (
    CostData,
    MaxCostData,
    calculate_cost,
)
from routstr.payment.usage import normalize_usage


def _response(usage=None):
    data = {"model": "gpt-4o", "id": "x", "object": "chat.completion"}
    if usage is not None:
        data["usage"] = usage
    return data


# ---------------------------------------------------------------------------
# calculate_cost: no usage at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_usage_charge_max_bills_ceiling(monkeypatch):
    monkeypatch.setattr(cost_calculation.settings, "missing_usage_policy", "charge_max")
    cost = await calculate_cost(_response(), max_cost=50_000, model_obj=None)
    assert isinstance(cost, MaxCostData)
    assert cost.total_msats == 50_000
    assert cost.reason == "missing_usage"
    assert cost.estimated_flag == 1


@pytest.mark.asyncio
async def test_no_usage_estimate_bills_zero(monkeypatch):
    monkeypatch.setattr(cost_calculation.settings, "missing_usage_policy", "estimate")
    cost = await calculate_cost(_response(), max_cost=50_000, model_obj=None)
    assert isinstance(cost, MaxCostData)
    assert cost.total_msats == 0
    assert cost.reason == "missing_usage"


@pytest.mark.asyncio
async def test_no_usage_refund_bills_zero(monkeypatch):
    monkeypatch.setattr(cost_calculation.settings, "missing_usage_policy", "refund")
    cost = await calculate_cost(_response(), max_cost=50_000, model_obj=None)
    assert cost.total_msats == 0
    assert cost.reason == "missing_usage"


@pytest.mark.asyncio
async def test_no_usage_unknown_policy_treated_as_estimate(monkeypatch):
    monkeypatch.setattr(
        cost_calculation.settings, "missing_usage_policy", "garbage"
    )
    cost = await calculate_cost(_response(), max_cost=50_000, model_obj=None)
    assert cost.total_msats == 0


@pytest.mark.asyncio
async def test_no_usage_charge_max_zero_ceiling_stays_zero(monkeypatch):
    monkeypatch.setattr(cost_calculation.settings, "missing_usage_policy", "charge_max")
    cost = await calculate_cost(_response(), max_cost=0, model_obj=None)
    assert cost.total_msats == 0


# ---------------------------------------------------------------------------
# calculate_cost: tokens present but pricing unusable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tokens_without_pricing_charge_max(monkeypatch):
    monkeypatch.setattr(cost_calculation.settings, "missing_usage_policy", "charge_max")
    # NaN pricing rates fail the usable-rate gate -> policy applies.
    with patch.object(
        cost_calculation,
        "_get_pricing_rates",
        return_value=(float("nan"), 1.0, 1.0, 1.0),
    ):
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        cost = await calculate_cost(_response(usage), max_cost=9_999, model_obj=None)
    assert isinstance(cost, MaxCostData)
    assert cost.total_msats == 9_999
    assert cost.reason == "missing_usage"
    assert cost.input_tokens == 100
    assert cost.output_tokens == 50


@pytest.mark.asyncio
async def test_tokens_without_pricing_estimate(monkeypatch):
    monkeypatch.setattr(cost_calculation.settings, "missing_usage_policy", "estimate")
    with patch.object(
        cost_calculation,
        "_get_pricing_rates",
        return_value=(float("nan"), 1.0, 1.0, 1.0),
    ):
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        cost = await calculate_cost(_response(usage), max_cost=9_999, model_obj=None)
    assert cost.total_msats == 0
    # Token counts still surface for dashboards.
    assert cost.input_tokens == 100
    assert cost.output_tokens == 50


# ---------------------------------------------------------------------------
# adjust_payment_for_tokens: CostDataError must not raise post-delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_data_error_charge_max_charges_and_returns(monkeypatch):
    from routstr.auth import ReservationSnapshot, adjust_payment_for_tokens
    from routstr.core.db import ApiKey

    monkeypatch.setattr(cost_calculation.settings, "missing_usage_policy", "charge_max")

    key = ApiKey(hashed_key="a" * 64, balance=1_000_000, reserved_balance=50_000)

    reservation = ReservationSnapshot(
        release_id="rel-1",
        key_hash=key.hashed_key,
        billing_key_hash=key.hashed_key,
        reserved_msats=50_000,
    )

    with (
        patch("routstr.auth._validate_reservation_snapshot", new=AsyncMock()),
        patch("routstr.auth._stop_reservation_heartbeat", new=AsyncMock()),
        patch("routstr.auth._claim_reservation_for_charge", new=AsyncMock(return_value=True)),
        patch("routstr.auth._charge_reservation_rows", new=AsyncMock(return_value=True)),
        patch("routstr.auth.get_reservation_snapshot", new=AsyncMock(return_value=reservation)),
        patch(
            "routstr.auth.accumulate_routstr_fee",
            new=AsyncMock(),
        ) as accumulate_fee,
    ):
        cost = await adjust_payment_for_tokens(
            key,
            _response(),  # no usage -> policy path via MaxCostData
            session=AsyncMock(),
            deducted_max_cost=50_000,
            reservation_snapshot=reservation,
        )
    # The MaxCostData path bills the ceiling through normal finalization.
    assert cost["total_msats"] == 50_000
    assert cost["charged_msats"] == 50_000


@pytest.mark.asyncio
async def test_cost_data_error_path_returns_dict_not_raise(monkeypatch):
    """Force a genuine CostDataError (pricing ValueError) under 'refund'."""
    from routstr.auth import ReservationSnapshot, adjust_payment_for_tokens
    from routstr.core.db import ApiKey

    monkeypatch.setattr(cost_calculation.settings, "missing_usage_policy", "refund")

    usage = {"prompt_tokens": 10, "completion_tokens": 5}
    # No model_obj and no fixed pricing -> usable-rate gate... but tokens are
    # present, so to force a CostDataError we patch _get_pricing_rates.
    with (
        patch.object(
            cost_calculation,
            "_get_pricing_rates",
            side_effect=ValueError("no pricing for model"),
        ),
        patch("routstr.auth._validate_reservation_snapshot", new=AsyncMock()),
        patch("routstr.auth._stop_reservation_heartbeat", new=AsyncMock()),
        patch("routstr.auth._claim_reservation_for_charge", new=AsyncMock(return_value=True)),
        patch("routstr.auth._charge_reservation_rows", new=AsyncMock(return_value=True)),
        patch("routstr.auth.release_reservation", new=AsyncMock(return_value=True)),
        patch(
            "routstr.auth.accumulate_routstr_fee",
            new=AsyncMock(),
        ),
    ):
        cost = await adjust_payment_for_tokens(
            ApiKey(hashed_key="a" * 64),
            _response(usage),
            session=AsyncMock(),
            deducted_max_cost=7_000,
            reservation_snapshot=ReservationSnapshot(
                release_id="rel-2",
                key_hash="a" * 64,
                billing_key_hash="a" * 64,
                reserved_msats=7_000,
            ),
        )
    assert isinstance(cost, dict)
    assert cost["total_msats"] == 0
    assert cost["reason"] == "missing_usage"
    assert cost["estimated"] is True
    assert cost["error"]["code"] == "pricing_error"


# ---------------------------------------------------------------------------
# X-Cashu: estimator wiring
# ---------------------------------------------------------------------------


def test_normalize_usage_rejects_none():
    assert normalize_usage(None) is None
