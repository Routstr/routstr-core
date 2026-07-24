"""Settlement bills the model and provider fee that actually served.

Covers ``calculate_cost``'s served-identity parameters: a passed ``model_obj``
is billed directly instead of re-deriving pricing from the response's model
string through the alias map (which yields the best-ranked candidate, not the
serving one), and a passed ``provider_fee`` is applied on the USD-cost path
instead of the best-ranked provider's fee. The string/alias fallbacks remain
for callers without routed identity.
"""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.payment.cost_calculation import CostData, calculate_cost
from routstr.payment.models import Architecture, Model, Pricing


def _make_model(
    model_id: str, prompt_sats: float, completion_sats: float
) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=64000,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="Other",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=prompt_sats, completion=completion_sats),
        sats_pricing=Pricing(prompt=prompt_sats, completion=completion_sats),
    )


WINNER = _make_model("dual-model", 0.001, 0.002)
SERVED = _make_model("dual-model", 0.005, 0.010)

RESPONSE = {
    "model": "dual-model",
    "usage": {
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "total_tokens": 1500,
    },
}


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> None:  # type: ignore[misc]
    with patch(
        "routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-4
    ):
        yield


@pytest.mark.asyncio
async def test_served_model_pricing_wins_over_alias_lookup() -> None:
    """With ``model_obj`` given, the alias map is not consulted for pricing."""
    with patch(
        "routstr.proxy.get_model_instance", return_value=WINNER
    ) as alias_lookup:
        result = await calculate_cost(
            dict(RESPONSE), max_cost=100_000, model_obj=SERVED
        )

    assert isinstance(result, CostData)
    # 1000/1000 * 5000 + 500/1000 * 10000 msats at the SERVED model's rates.
    assert result.total_msats == 10_000
    alias_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_string_fallback_still_prices_without_model_obj() -> None:
    """Callers without routed identity keep the alias-map string lookup."""
    with patch("routstr.proxy.get_model_instance", return_value=WINNER):
        result = await calculate_cost(dict(RESPONSE), max_cost=100_000)

    assert isinstance(result, CostData)
    assert result.total_msats == 2_000


@pytest.mark.asyncio
async def test_usd_cost_path_applies_given_provider_fee() -> None:
    """The USD-cost path bills the serving provider's fee when supplied."""
    from unittest.mock import Mock

    response = dict(RESPONSE)
    response["usage"] = dict(RESPONSE["usage"], cost=0.001)  # type: ignore[arg-type]

    best_ranked = Mock(provider_fee=1.0)
    with patch(
        "routstr.proxy.get_provider_for_model", return_value=[best_ranked]
    ):
        result = await calculate_cost(
            response, max_cost=100_000, model_obj=SERVED, provider_fee=1.5
        )

    assert isinstance(result, CostData)
    # 0.001 USD * fee 1.5 / 0.0005 USD-per-sat = 3 sats = 3000 msats.
    assert result.total_msats == 3_000


@pytest.mark.asyncio
async def test_usd_cost_path_falls_back_to_best_ranked_fee() -> None:
    """Without a supplied fee, the alias-map provider lookup still applies."""
    from unittest.mock import Mock

    response = dict(RESPONSE)
    response["usage"] = dict(RESPONSE["usage"], cost=0.001)  # type: ignore[arg-type]

    best_ranked = Mock(provider_fee=2.0)
    with patch(
        "routstr.proxy.get_provider_for_model", return_value=[best_ranked]
    ):
        result = await calculate_cost(response, max_cost=100_000)

    assert isinstance(result, CostData)
    assert result.total_msats == 4_000


@pytest.mark.asyncio
async def test_x_cashu_cost_uses_served_model_not_upstream_echo() -> None:
    """``get_x_cashu_cost`` bills the routed model, not the raw model echo.

    X-Cashu handlers do not rewrite the upstream's echoed model string, so
    without the routed model the settle would look up whatever wire name the
    upstream reported. With ``model_obj`` given, the echo must be irrelevant.
    """
    from routstr.upstream import GenericUpstreamProvider

    provider = GenericUpstreamProvider("http://upstream.example", "key", 1.0)
    response = dict(RESPONSE, model="totally-unknown-wire-name")

    with patch(
        "routstr.proxy.get_model_instance", return_value=WINNER
    ) as alias_lookup:
        cost = await provider.get_x_cashu_cost(
            response, max_cost_for_model=100_000, model_obj=SERVED
        )

    assert cost is not None
    assert cost.total_msats == 10_000
    alias_lookup.assert_not_called()
