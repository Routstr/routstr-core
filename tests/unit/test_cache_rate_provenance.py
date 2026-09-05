"""Cover the record of where each cache rate came from.

When no cache rate is configured for a model, ``_get_pricing_rates`` falls back
to the full prompt rate for both cache reads and writes.  The rate that results
is indistinguishable from a real one — both arrive as a float — so the fallback
is invisible to anything downstream, including the settlement log, which
reported ``pricing_source`` for the prompt and completion rates and said
nothing about the cache ones.

These tests pin that the existing "Applied model-specific pricing" record now
carries a source for each cache rate, that ``inferred`` appears only when
neither the catalogue nor the operator supplied one, that the catalogue's own
label is used when it did, and that none of this changes what a caller is
charged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from routstr.core.settings import settings
from routstr.payment.cost_calculation import CostData, calculate_cost
from routstr.payment.models import Architecture, Model, Pricing


def _model(
    *,
    fwd_id: str = "test-model",
    prompt: float = 1.0e-7,
    completion: float = 2.0e-7,
    cache_read: float = 0.0,
    cache_write: float = 0.0,
) -> Model:
    """Build a minimal ``Model`` with usable sats_pricing."""
    return Model(
        id=fwd_id,
        name=fwd_id,
        created=0,
        description="",
        context_length=128_000,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="unknown",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=prompt,
            completion=completion,
            input_cache_read=cache_read,
            input_cache_write=cache_write,
        ),
        sats_pricing=Pricing(
            prompt=prompt / 5.0e-5,
            completion=completion / 5.0e-5,
            input_cache_read=cache_read / 5.0e-5 if cache_read else 0.0,
            input_cache_write=cache_write / 5.0e-5 if cache_write else 0.0,
        ),
        enabled=True,
    )


def _usage_response(model_id: str) -> dict[str, Any]:
    """A response carrying both cache-read and cache-write tokens."""
    return {
        "model": model_id,
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 100},
            "cache_creation_input_tokens": 30,
        },
    }


@pytest.fixture(autouse=True)
def _pin_sats() -> Iterator[None]:
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-5):
        yield


@pytest.fixture(autouse=True)
def _attach_caplog(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Attach caplog handler to the cost_calculation logger.

    Routstr loggers use ``propagate=False`` after ``setup_logging`` runs,
    so the root-level handler caplog attaches cannot see their records.
    """
    cost_logger = logging.getLogger("routstr.payment.cost_calculation")
    cost_logger.addHandler(caplog.handler)
    yield
    cost_logger.removeHandler(caplog.handler)


def _pricing_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.getMessage() == "Applied model-specific pricing"
    ]


def _only_pricing_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    records = _pricing_records(caplog)
    assert len(records) == 1, (
        f"expected one pricing record per settlement, got {len(records)}"
    )
    return records[0]


# -- the source of each cache rate ---------------------------------------------


@pytest.mark.asyncio
async def test_missing_cache_rates_are_recorded_as_inferred(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither rate configured → both are the prompt rate under another name."""
    model = _model(cache_read=0.0, cache_write=0.0)

    with caplog.at_level(logging.INFO):
        await calculate_cost(
            _usage_response(model.id), max_cost=100_000, model_obj=model
        )

    record = _only_pricing_record(caplog)
    assert record.cache_read_source == "inferred"  # type: ignore[attr-defined]
    assert record.cache_write_source == "inferred"  # type: ignore[attr-defined]

    # The claim the label makes: the rate really is the prompt rate.
    assert record.cache_read_price_msats_per_1k == pytest.approx(  # type: ignore[attr-defined]
        record.input_price_msats_per_1k  # type: ignore[attr-defined]
    )
    assert record.cache_write_price_msats_per_1k == pytest.approx(  # type: ignore[attr-defined]
        record.input_price_msats_per_1k  # type: ignore[attr-defined]
    )


@pytest.mark.asyncio
async def test_configured_cache_rates_keep_the_catalogue_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rate the node was given is labelled like the rates beside it."""
    model = _model(cache_read=1.0e-8, cache_write=1.25e-7)

    with caplog.at_level(logging.INFO):
        await calculate_cost(
            _usage_response(model.id), max_cost=100_000, model_obj=model
        )

    record = _only_pricing_record(caplog)
    assert record.pricing_source == "configured"  # type: ignore[attr-defined]
    assert record.cache_read_source == "configured"  # type: ignore[attr-defined]
    assert record.cache_write_source == "configured"  # type: ignore[attr-defined]
    assert record.cache_read_price_msats_per_1k != pytest.approx(  # type: ignore[attr-defined]
        record.input_price_msats_per_1k  # type: ignore[attr-defined]
    )


@pytest.mark.asyncio
async def test_the_two_cache_rates_are_reported_independently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One configured rate must not vouch for the other.

    Cache reads and writes are supplied separately and go missing separately —
    a catalogue that knows the read rate routinely omits the write one.
    """
    model = _model(cache_read=1.0e-8, cache_write=0.0)

    with caplog.at_level(logging.INFO):
        await calculate_cost(
            _usage_response(model.id), max_cost=100_000, model_obj=model
        )

    record = _only_pricing_record(caplog)
    assert record.cache_read_source == "configured"  # type: ignore[attr-defined]
    assert record.cache_write_source == "inferred"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_source_is_recorded_even_with_no_cache_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The label describes the rate, not the request that happened to use it.

    An operator asking "is this model priced correctly?" needs the answer
    before the traffic that would expose it, not after.
    """
    model = _model(cache_read=0.0, cache_write=0.0)

    with caplog.at_level(logging.INFO):
        await calculate_cost(
            {
                "model": model.id,
                "usage": {"prompt_tokens": 200, "completion_tokens": 50},
            },
            max_cost=100_000,
            model_obj=model,
        )

    record = _only_pricing_record(caplog)
    assert record.cache_read_source == "inferred"  # type: ignore[attr-defined]
    assert record.cache_write_source == "inferred"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_litellm_priced_model_reports_the_litellm_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A model with no catalogue pricing is rated from litellm, and says so."""
    model = _model(fwd_id="litellm-only")
    model.sats_pricing = None

    litellm_entry = {
        "input_cost_per_token": 1.0e-7,
        "output_cost_per_token": 2.0e-7,
        "cache_read_input_token_cost": 1.0e-8,
    }
    with (
        patch("routstr.payment.models.litellm_cost_entry", return_value=litellm_entry),
        caplog.at_level(logging.INFO),
    ):
        await calculate_cost(
            _usage_response(model.id),
            max_cost=100_000,
            model_obj=model,
            provider_fee=1.0,
        )

    record = _only_pricing_record(caplog)
    assert record.pricing_source == "litellm"  # type: ignore[attr-defined]
    assert record.cache_read_source == "litellm"  # type: ignore[attr-defined]
    # litellm's entry has no cache-write rate, so that one is still invented.
    assert record.cache_write_source == "inferred"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_fixed_pricing_records_no_rate_provenance(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fixed_pricing`` is a flat rate with no cache concept to be missing.

    It short-circuits before any rate is resolved, so there is no pricing
    record at all — nothing was sourced, and nothing is claimed.
    """
    monkeypatch.setattr(settings, "fixed_pricing", True)
    monkeypatch.setattr(settings, "fixed_per_1k_input_tokens", 0.001)
    monkeypatch.setattr(settings, "fixed_per_1k_output_tokens", 0.001)

    model = _model(cache_read=0.0, cache_write=0.0)

    with caplog.at_level(logging.INFO):
        await calculate_cost(
            _usage_response(model.id), max_cost=100_000, model_obj=model
        )

    assert _pricing_records(caplog) == []


# -- billing-invariance --------------------------------------------------------


@pytest.mark.asyncio
async def test_charged_amount_unchanged() -> None:
    """Recording where a rate came from must not change what it charges.

    Pin the exact cost the pre-change code produces for a request with cached
    tokens and no configured cache rates.
    """
    model = _model(cache_read=0.0, cache_write=0.0)

    cost = await calculate_cost(
        _usage_response(model.id), max_cost=100_000, model_obj=model
    )
    assert isinstance(cost, CostData)

    # prompt rate: 1e-7 / 5e-5 = 0.002 sats/token -> 2000 msats/1k
    # completion rate: 2e-7 / 5e-5 = 0.004 sats/token -> 4000 msats/1k
    #
    # `input_msats` is the whole input side, cache included; the two cache
    # msats fields are components of it, not additions to it:
    #   non-cached  70 tokens x 2000/1000 =  140 msats
    #   cache read 100 tokens x 2000/1000 =  200 msats
    #   cache write 30 tokens x 2000/1000 =   60 msats
    #                                       --------
    #   input                                400 msats
    #   output      50 tokens x 4000/1000 =  200 msats
    #                                       --------
    #   total                                600 msats
    assert cost.total_msats == 600
    assert cost.input_msats == 400
    assert cost.output_msats == 200
    assert cost.cache_read_msats == 200
    assert cost.cache_creation_msats == 60
    assert cost.base_msats == 0

    # The token fields do not mirror the msats fields: `input_tokens` is the
    # non-cached remainder (200 - 100 - 30), where `input_msats` is the total.
    assert cost.input_tokens == 70
    assert cost.output_tokens == 50
    assert cost.cache_read_input_tokens == 100
    assert cost.cache_creation_input_tokens == 30
