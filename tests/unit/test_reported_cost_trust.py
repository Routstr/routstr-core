"""Tests for the per-provider trust policy on upstream-reported cost.

An upstream that names its own price pre-empts token pricing entirely, and the
bearer overrun path will then spend whatever of the key's balance is not held
by a sibling reservation. Only provider types we explicitly approve may do
that; everything else settles on token pricing.
"""

import ast
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

from routstr.core.settings import settings
from routstr.payment import cost_calculation
from routstr.payment.cost_calculation import CostData, calculate_cost

# 1000 input + 500 output tokens at the fixture rates below.
TOKEN_PRICED_MSATS = 20_000
# 1.0 USD at the patched sats price, before any provider fee.
REPORTED_COST_MSATS = 20_000_000


@pytest.fixture(autouse=True)
def fixed_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fixed_pricing", True)
    monkeypatch.setattr(settings, "fixed_per_1k_input_tokens", 10)
    monkeypatch.setattr(settings, "fixed_per_1k_output_tokens", 20)


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> None:  # type: ignore[misc]
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-5):
        yield


@pytest.fixture(autouse=True)
def unit_provider_fee() -> None:  # type: ignore[misc]
    with patch(
        "routstr.payment.cost_calculation._resolve_provider_fee", return_value=1.0
    ):
        yield


def _response(**usage_extra: object) -> dict:
    return {
        "model": "gpt-4",
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500, **usage_extra},
    }


@pytest.fixture
def cost_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:  # type: ignore[misc]
    logger = logging.getLogger("routstr.payment.cost_calculation")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING)
    yield caplog
    logger.removeHandler(caplog.handler)


def _warned(caplog: pytest.LogCaptureFixture, needle: str) -> bool:
    return any(
        needle in rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
    )


@pytest.mark.asyncio
async def test_untrusted_provider_falls_back_to_token_pricing() -> None:
    result = await calculate_cost(_response(cost=1.0), max_cost=100_000)

    assert isinstance(result, CostData)
    assert result.total_msats == TOKEN_PRICED_MSATS


@pytest.mark.asyncio
async def test_trusted_provider_bills_the_reported_cost() -> None:
    result = await calculate_cost(
        _response(cost=1.0), max_cost=100_000, trusts_reported_cost=True
    )

    assert isinstance(result, CostData)
    assert result.total_msats == REPORTED_COST_MSATS


@pytest.mark.asyncio
async def test_untrusted_huge_reported_cost_cannot_exceed_token_pricing() -> None:
    result = await calculate_cost(_response(cost=1e9), max_cost=100_000)

    assert isinstance(result, CostData)
    assert result.total_msats == TOKEN_PRICED_MSATS


@pytest.mark.asyncio
async def test_untrusted_reported_cost_is_rejected_at_the_root() -> None:
    response = _response()
    response["cost"] = 1.0

    result = await calculate_cost(response, max_cost=100_000)

    assert isinstance(result, CostData)
    assert result.total_msats == TOKEN_PRICED_MSATS


@pytest.mark.asyncio
async def test_untrusted_reported_cost_is_rejected_in_cost_details() -> None:
    result = await calculate_cost(
        _response(cost_details={"total_cost": 1.0}), max_cost=100_000
    )

    assert isinstance(result, CostData)
    assert result.total_msats == TOKEN_PRICED_MSATS


@pytest.mark.asyncio
async def test_trusted_reported_cost_is_honoured_in_cost_details() -> None:
    result = await calculate_cost(
        _response(cost_details={"total_cost": 1.0}),
        max_cost=100_000,
        trusts_reported_cost=True,
    )

    assert isinstance(result, CostData)
    assert result.total_msats == REPORTED_COST_MSATS


@pytest.mark.parametrize(
    "reported",
    [-1.0, float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf"],
)
def test_coerce_usd_rejects_non_finite_and_negative(reported: object) -> None:
    """Infinity must be rejected at coercion, not survive into ``math.ceil``
    and get swallowed by the USD path's broad exception handler."""
    assert cost_calculation._coerce_usd(reported) == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reported",
    [-1.0, float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf"],
)
async def test_non_finite_or_negative_reported_cost_is_ignored(
    reported: object,
) -> None:
    result = await calculate_cost(
        _response(cost=reported), max_cost=100_000, trusts_reported_cost=True
    )

    assert isinstance(result, CostData)
    assert result.total_msats == TOKEN_PRICED_MSATS


@pytest.mark.asyncio
async def test_over_reported_cost_is_flagged(
    cost_log: pytest.LogCaptureFixture,
) -> None:
    result = await calculate_cost(
        _response(cost=1.0), max_cost=100_000, trusts_reported_cost=True
    )

    assert isinstance(result, CostData)
    assert result.total_msats == REPORTED_COST_MSATS
    assert _warned(cost_log, "implausible against token pricing")


@pytest.mark.asyncio
async def test_under_reported_cost_is_flagged(
    cost_log: pytest.LogCaptureFixture,
) -> None:
    result = await calculate_cost(
        _response(cost=0.00000001), max_cost=100_000, trusts_reported_cost=True
    )

    assert isinstance(result, CostData)
    assert result.total_msats < TOKEN_PRICED_MSATS
    assert _warned(cost_log, "implausible against token pricing")


@pytest.mark.asyncio
async def test_cost_far_above_the_reservation_is_flagged(
    cost_log: pytest.LogCaptureFixture,
) -> None:
    result = await calculate_cost(
        _response(cost=1.0), max_cost=1_000, trusts_reported_cost=True
    )

    assert isinstance(result, CostData)
    assert _warned(cost_log, "far above the reservation")


@pytest.mark.asyncio
async def test_plausible_reported_cost_is_not_flagged(
    cost_log: pytest.LogCaptureFixture,
) -> None:
    # 20_000 msats — exactly what token pricing would charge.
    result = await calculate_cost(
        _response(cost=0.001), max_cost=100_000, trusts_reported_cost=True
    )

    assert isinstance(result, CostData)
    assert result.total_msats == TOKEN_PRICED_MSATS
    assert not _warned(cost_log, "implausible against token pricing")
    assert not _warned(cost_log, "far above the reservation")


@pytest.mark.asyncio
async def test_ppq_byok_still_bills_above_the_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PPQ.AI BYOK payload from issue #615 legitimately settles ~9x the
    reservation; the trust policy must not clamp it."""
    monkeypatch.setattr(settings, "fixed_per_1k_input_tokens", 0.001)
    monkeypatch.setattr(settings, "fixed_per_1k_output_tokens", 0.001)
    response = {
        "model": "glm-5.2-fast",
        "usage": {
            "prompt_tokens": 164371,
            "completion_tokens": 99,
            "cost": 0.002260057305,
            "is_byok": True,
            "prompt_tokens_details": {"cached_tokens": 159301},
            "cost_details": {
                "upstream_inference_cost": 0.04475361,
                "upstream_inference_prompt_cost": 0.04410021,
                "upstream_inference_completions_cost": 0.0006534,
            },
        },
    }

    result = await calculate_cost(response, max_cost=100_000, trusts_reported_cost=True)

    assert isinstance(result, CostData)
    assert result.total_msats == 940274


def test_base_provider_does_not_trust_reported_cost() -> None:
    from routstr.upstream.base import BaseUpstreamProvider
    from routstr.upstream.generic import GenericUpstreamProvider
    from routstr.upstream.routstr import RoutstrUpstreamProvider

    assert BaseUpstreamProvider.trusts_reported_cost is False
    assert GenericUpstreamProvider.trusts_reported_cost is False
    assert RoutstrUpstreamProvider.trusts_reported_cost is False


def test_approved_provider_types_trust_reported_cost() -> None:
    from routstr.upstream.openrouter import OpenRouterUpstreamProvider
    from routstr.upstream.ppqai import PPQAIUpstreamProvider

    assert OpenRouterUpstreamProvider.trusts_reported_cost is True
    assert PPQAIUpstreamProvider.trusts_reported_cost is True


def test_every_settlement_site_passes_the_trust_flag() -> None:
    """Streaming and non-streaming settlement must agree: a path that forgets
    the flag silently reverts to trusting whatever the upstream reported."""
    source = Path(cost_calculation.__file__).parent.parent / "upstream" / "base.py"
    tree = ast.parse(source.read_text())

    missing = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("adjust_payment_for_tokens", "calculate_cost")
        and not any(kw.arg == "trusts_reported_cost" for kw in node.keywords)
    ]

    assert missing == []
