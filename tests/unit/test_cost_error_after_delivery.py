"""A pricing failure after the upstream served content must not raise a 400."""

from unittest.mock import AsyncMock, patch

import pytest

from routstr.auth import ReservationSnapshot, adjust_payment_for_tokens
from routstr.core.db import ApiKey
from routstr.payment import cost_calculation


@pytest.mark.asyncio
async def test_cost_data_error_releases_without_raising() -> None:
    key_hash = "a" * 64
    reservation = ReservationSnapshot(
        release_id="rel-1",
        key_hash=key_hash,
        billing_key_hash=key_hash,
        reserved_msats=7_000,
    )
    with (
        patch.object(
            cost_calculation,
            "_get_pricing_rates",
            side_effect=ValueError("no pricing for model"),
        ),
        patch("routstr.auth._validate_reservation_snapshot", new=AsyncMock()),
        patch("routstr.auth._stop_reservation_heartbeat", new=AsyncMock()),
        patch(
            "routstr.auth._claim_reservation_for_charge",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "routstr.auth._charge_reservation_rows", new=AsyncMock(return_value=True)
        ),
        patch("routstr.auth.accumulate_routstr_fee", new=AsyncMock()),
    ):
        cost = await adjust_payment_for_tokens(
            ApiKey(hashed_key=key_hash),
            {"model": "gpt-4o", "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            session=AsyncMock(),
            deducted_max_cost=7_000,
            reservation_snapshot=reservation,
        )

    assert cost["total_msats"] == 0
    assert cost["charged_msats"] == 0
