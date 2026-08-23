import os
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.core.settings import settings  # noqa: E402
from routstr.payment.helpers import get_max_cost_for_model  # noqa: E402


async def test_get_max_cost_for_model_known() -> None:
    from routstr.payment.models import Pricing

    # Mock DB session behavior
    mock_session = AsyncMock()

    # Mock upstream provider rows
    mock_provider_result = Mock()
    mock_provider_result.all = Mock(return_value=[])

    # Mock model row with proper JSON fields
    row = Mock()
    row.id = "gpt-4"
    row.name = "GPT-4"
    row.created = 1234567890
    row.description = "Test model"
    row.context_length = 8192
    row.architecture = '{"modality": "text", "input_modalities": ["text"], "output_modalities": ["text"], "tokenizer": "gpt", "instruct_type": null}'
    row.pricing = '{"prompt": 0.0, "completion": 0.0, "request": 0.0, "image": 0.0, "web_search": 0.0, "internal_reasoning": 0.0, "max_cost": 0.0}'
    row.per_request_limits = None
    row.top_provider = None
    row.enabled = True
    row.upstream_provider_id = 1

    # Mock the exec results to return model row when querying for override
    def mock_exec(query: Any) -> Any:
        result = Mock()
        result.first = Mock(return_value=row)
        result.all = Mock(return_value=[row])
        return result

    mock_session.exec = Mock(side_effect=mock_exec)

    # Mock get for UpstreamProviderRow
    mock_provider = Mock()
    mock_provider.provider_fee = 1.01
    mock_session.get = Mock(return_value=mock_provider)

    # Mock the model with sats_pricing
    mock_pricing = Pricing(
        prompt=0.0,
        completion=0.0,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
    )
    mock_model = Mock()
    mock_model.sats_pricing = mock_pricing

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 0):
            cost = await get_max_cost_for_model(
                "gpt-4", session=mock_session, model_obj=mock_model
            )
            assert cost == 500000  # 500 sats * 1000 = msats


async def test_get_max_cost_for_model_unknown() -> None:
    mock_session = AsyncMock()

    # Mock the exec results to return no model override
    async def async_mock_exec(query: Any) -> Any:
        result = Mock()
        result.first = Mock(return_value=None)
        result.all = Mock(return_value=[])
        return result

    mock_session.exec = AsyncMock(side_effect=async_mock_exec)
    mock_session.get = AsyncMock(return_value=None)

    # Mock get_upstreams to return empty list
    with patch("routstr.proxy.get_upstreams", return_value=[]):
        with patch.object(settings, "fixed_cost_per_request", 100):
            with patch.object(settings, "tolerance_percentage", 0):
                cost = await get_max_cost_for_model(
                    "unknown-model", session=mock_session, model_obj=None
                )
                assert cost == 100000


async def test_get_max_cost_for_model_disabled() -> None:
    mock_session = AsyncMock()
    with patch.object(settings, "fixed_pricing", True):
        with patch.object(settings, "fixed_cost_per_request", 200):
            with patch.object(settings, "tolerance_percentage", 0):
                cost = await get_max_cost_for_model("any-model", session=mock_session)
                assert cost == 200000


async def test_get_max_cost_for_model_tolerance() -> None:
    from routstr.payment.models import Pricing

    mock_session = AsyncMock()

    # Mock the model with sats_pricing
    mock_pricing = Pricing(
        prompt=0.0,
        completion=0.0,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
    )
    mock_model = Mock()
    mock_model.sats_pricing = mock_pricing

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 10):
            cost = await get_max_cost_for_model(
                "gpt-4", session=mock_session, model_obj=mock_model
            )
            assert cost == 450000  # 500 sats * 1000 * 0.9 = 450000


async def test_discounted_max_cost_floors_at_min_request_msat() -> None:
    from routstr.payment.helpers import calculate_discounted_max_cost

    pricing = Mock()
    pricing.prompt = 0.001
    pricing.completion = 0.001
    pricing.max_prompt_cost = 100.0
    pricing.max_completion_cost = 100.0

    model_obj = Mock()
    model_obj.sats_pricing = pricing
    model_obj.top_provider = None
    model_obj.context_length = None

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
    ):
        cost = await calculate_discounted_max_cost(150_000, body, model_obj)

    assert cost == 1000


async def test_discounted_max_cost_honors_explicit_max_tokens_override() -> None:
    """The ``max_tokens`` override parameter (opaque/EHBP body) is honored.

    Body is empty (encrypted) so only the override can drive the discount.
    completion max budget = 100 sats; 80k tokens * 0.001 = 80 sats used => the
    required balance is trimmed from 100 sats to 80 sats (80000 msats).
    """
    from routstr.payment.helpers import calculate_discounted_max_cost

    pricing = Mock()
    pricing.prompt = 0.001
    pricing.completion = 0.001
    pricing.max_prompt_cost = 0.0  # no prompt-side discount to keep math clean
    pricing.max_completion_cost = 100.0

    model_obj = Mock()
    model_obj.sats_pricing = pricing
    model_obj.top_provider = None
    model_obj.context_length = None

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
    ):
        cost = await calculate_discounted_max_cost(
            100_000, {}, model_obj, max_tokens=80_000
        )

    # full max_cost (100 sats = 100000 msats) minus completion discount
    # (100 - 80 = 20 sats = 20000 msats) => 80000 msats
    assert cost == 80_000


async def test_discounted_max_cost_body_max_completion_tokens_fallback() -> None:
    """Body ``max_completion_tokens`` is honoured when no override is set."""
    from routstr.payment.helpers import calculate_discounted_max_cost

    pricing = Mock()
    pricing.prompt = 0.001
    pricing.completion = 0.001
    pricing.max_prompt_cost = 0.0
    pricing.max_completion_cost = 100.0

    model_obj = Mock()
    model_obj.sats_pricing = pricing
    model_obj.top_provider = None
    model_obj.context_length = None

    body = {"max_completion_tokens": 80_000}

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
    ):
        cost = await calculate_discounted_max_cost(100_000, body, model_obj)

    assert cost == 80_000


async def test_discounted_max_cost_override_takes_precedence_over_body() -> None:
    """An explicit override wins over any body max_tokens value."""
    from routstr.payment.helpers import calculate_discounted_max_cost

    pricing = Mock()
    pricing.prompt = 0.001
    pricing.completion = 0.001
    pricing.max_prompt_cost = 0.0
    pricing.max_completion_cost = 100.0

    model_obj = Mock()
    model_obj.sats_pricing = pricing
    model_obj.top_provider = None
    model_obj.context_length = None

    # Body says 10 tokens (would floor to min_request_msat = 1000); the
    # 80000-token override must win, so the result is 80000 msats.
    body = {"max_tokens": 10}

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
    ):
        cost = await calculate_discounted_max_cost(
            100_000, body, model_obj, max_tokens=80_000
        )

    assert cost == 80_000
