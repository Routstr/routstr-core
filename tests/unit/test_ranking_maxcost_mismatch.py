"""Ranking-vs-reservation mismatch for same-model multi-provider setups.

``calculate_model_cost_score`` weights a typical request and ignores
``context_length``, while the balance gate reserves on the context-based
``sats_pricing.max_cost``. When two providers serve the same model these can
disagree, so the "cheapest" advertised provider may demand a far larger
reservation and surprise the client with a 402. Ranking must therefore use the
same context-based ceiling as the gate.
"""

import os
from unittest.mock import Mock

import pytest

os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.algorithm import (  # noqa: E402
    calculate_model_cost_score,
    create_model_mappings,
)
from routstr.payment.helpers import get_max_cost_for_model  # noqa: E402
from routstr.payment.models import Architecture, Model, Pricing  # noqa: E402
from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402


def _arch() -> Architecture:
    return Architecture(
        modality="text",
        input_modalities=["text"],
        output_modalities=["text"],
        tokenizer="gpt",
        instruct_type=None,
    )


def _model(
    model_id: str,
    prompt: float,
    completion: float,
    context_length: int,
    max_cost_sats: float,
) -> Model:
    """Build a Model whose sats_pricing.max_cost mirrors the context-based gate."""
    pricing = Pricing(
        prompt=prompt,
        completion=completion,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
    )
    model = Model(
        id=model_id,
        name=model_id,
        created=1,
        description="",
        context_length=context_length,
        architecture=_arch(),
        pricing=pricing,
    )
    model.sats_pricing = Pricing(
        prompt=prompt,
        completion=completion,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_prompt_cost=context_length * prompt,
        max_completion_cost=context_length * completion,
        max_cost=max_cost_sats,
    )
    return model


def _provider(name: str, db_id: int, models: list[Model]) -> Mock:
    provider = Mock()
    provider.provider_type = name
    provider.base_url = f"https://{name}.example/v1"
    provider.db_id = db_id
    provider.upstream_name = name
    provider.provider_fee = 1.0
    provider.get_cached_models.return_value = models
    return provider


# Provider LOW-SCORE: cheaper per typical token, but a huge context window makes
# its context-based max_cost enormous.
_LOW_SCORE = _model(
    "shared-model",
    prompt=0.001,
    completion=0.001,
    context_length=1_000_000,
    max_cost_sats=1000.0,
)
# Provider LOW-RESERVE: pricier per typical token, but a small context window
# makes its reservation ceiling tiny.
_LOW_RESERVE = _model(
    "shared-model",
    prompt=0.002,
    completion=0.002,
    context_length=8_000,
    max_cost_sats=16.0,
)


def test_ranking_metric_and_reservation_metric_disagree() -> None:
    """The two cost metrics rank the same two providers in opposite order."""
    assert calculate_model_cost_score(_LOW_SCORE) < calculate_model_cost_score(
        _LOW_RESERVE
    )
    assert _max_cost(_LOW_SCORE) > _max_cost(_LOW_RESERVE)


@pytest.mark.asyncio
async def test_get_max_cost_uses_context_based_max_cost_not_score() -> None:
    """The balance gate reserves on max_cost, so the low-score model costs more."""
    session = Mock()
    low_score_reserve = await get_max_cost_for_model(
        "shared-model", session=session, model_obj=_LOW_SCORE
    )
    low_reserve_reserve = await get_max_cost_for_model(
        "shared-model", session=session, model_obj=_LOW_RESERVE
    )
    # The "cheapest" model (by ranking score) demands the LARGER reservation.
    assert low_score_reserve > low_reserve_reserve


def _max_cost(model: Model) -> float:
    assert model.sats_pricing is not None
    assert model.sats_pricing.max_cost is not None
    return model.sats_pricing.max_cost


def _both_orderings() -> list[list[BaseUpstreamProvider]]:
    low_score = _provider("low-score", 1, [_LOW_SCORE])
    low_reserve = _provider("low-reserve", 2, [_LOW_RESERVE])
    return [[low_score, low_reserve], [low_reserve, low_score]]


def test_catalog_advertises_the_lower_reservation_provider() -> None:
    """Catalog and routing pick the provider with the smaller reservation,
    regardless of upstream iteration order."""
    for providers in _both_orderings():
        _, provider_map, unique_models = create_model_mappings(
            upstreams=providers,
            overrides_by_key={},
            disabled_model_keys=set(),
        )
        selected_model, selected_provider = provider_map["shared-model"][0]
        assert selected_provider.provider_type == "low-reserve"
        assert _max_cost(selected_model) == 16.0
        assert _max_cost(unique_models["shared-model"]) == 16.0


def test_selected_candidate_has_minimal_reservation() -> None:
    """The first-tried candidate never demands a larger reservation than
    another available candidate for the same model."""
    for providers in _both_orderings():
        _, provider_map, _ = create_model_mappings(
            upstreams=providers,
            overrides_by_key={},
            disabled_model_keys=set(),
        )
        candidates = provider_map["shared-model"]
        selected_max_cost = _max_cost(candidates[0][0])
        min_max_cost = min(_max_cost(m) for m, _ in candidates)
        assert selected_max_cost == min_max_cost
