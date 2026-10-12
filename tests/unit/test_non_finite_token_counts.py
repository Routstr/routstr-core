"""Non-finite token counts must not crash the billing path.

``json.loads`` accepts bare ``Infinity``/``NaN`` and overflows ``1e999`` to
``inf``, so an upstream can put them on the wire. ``int()`` raises on both.
"""

import json
import os
from typing import Any
from unittest.mock import patch

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

from routstr.core.settings import settings
from routstr.payment.cost_calculation import CostData, calculate_cost
from routstr.payment.usage import parse_token_count

NON_FINITE = [
    float("inf"),
    float("-inf"),
    float("nan"),
    1e999,
    "Infinity",
    "NaN",
    "-Infinity",
    "1e999",
]


@pytest.fixture(autouse=True)
def _fixed_pricing(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(settings, "fixed_pricing", True)
    monkeypatch.setattr(settings, "fixed_per_1k_input_tokens", 0.001)
    monkeypatch.setattr(settings, "fixed_per_1k_output_tokens", 0.001)
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-5):
        yield


@pytest.mark.parametrize("value", NON_FINITE)
def test_parse_token_count_rejects_non_finite(value: Any) -> None:
    assert parse_token_count(value) == 0


def test_parse_token_count_still_parses_ordinary_values() -> None:
    assert parse_token_count(42) == 42
    assert parse_token_count("42") == 42
    assert parse_token_count(42.9) == 42
    assert parse_token_count("42.9") == 42
    assert parse_token_count(True) == 0
    assert parse_token_count(-5) == 0
    assert parse_token_count("not a number") == 0
    assert parse_token_count(None) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        '{"usage": {"prompt_tokens": Infinity, "completion_tokens": 10}}',
        '{"usage": {"prompt_tokens": 1e999, "completion_tokens": 10}}',
        '{"usage": {"prompt_tokens": 1000, "completion_tokens": NaN}}',
        '{"usage": {"prompt_tokens": "Infinity", "completion_tokens": "10"}}',
    ],
)
async def test_billing_settles_a_response_with_non_finite_usage(raw: str) -> None:
    """The billing entry point parses the decoded wire body without raising
    and bills only the finite component."""
    response = json.loads(raw)

    result = await calculate_cost(response, max_cost=100_000)

    assert isinstance(result, CostData)
    usage = response["usage"]
    finite_input = parse_token_count(usage["prompt_tokens"])
    finite_output = parse_token_count(usage["completion_tokens"])
    assert result.input_tokens == finite_input
    assert result.output_tokens == finite_output
    assert 0 <= result.total_msats <= 100_000
