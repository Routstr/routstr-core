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


def test_estimate_prompt_tokens_counts_every_string_in_the_body() -> None:
    from routstr.payment.helpers import estimate_prompt_tokens, estimate_tokens

    hidden = "x" * 3_000  # ~1000 tokens of prompt hidden from the text estimator
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "description": hidden,
                    "parameters": {"type": "object", "properties": {hidden: {}}},
                },
            }
        ],
    }

    # The text-only estimator sees almost nothing; the conservative one sees it.
    assert estimate_tokens(body["messages"]) < 10
    assert estimate_prompt_tokens(body) >= 1_000

    # No carve-out is exempt: neither a caller-chosen key name nor a caller-chosen
    # value prefix can buy a discount, so both still count in full.
    assert estimate_prompt_tokens({"tools": [{"data": hidden}]}) >= 1_000
    assert estimate_prompt_tokens({"system": "data:" + hidden}) >= 1_000
    assert estimate_prompt_tokens({"prompt": [[1, 2], [3, 4, 5]]}) >= 5


async def test_discount_counts_legacy_token_id_prompt() -> None:
    from routstr.payment.helpers import calculate_discounted_max_cost

    pricing = Mock()
    pricing.prompt = 0.001
    pricing.completion = 0.0
    pricing.max_prompt_cost = 50.0
    pricing.max_completion_cost = 0.0

    model_obj = Mock()
    model_obj.sats_pricing = pricing
    model_obj.top_provider = None
    model_obj.context_length = None

    body = {"model": "test-model", "prompt": list(range(50_000)), "max_tokens": 0}
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
    ):
        cost = await calculate_discounted_max_cost(50_000, body, model_obj)

    assert cost == 50_000


async def test_discount_cannot_be_dodged_by_hiding_prompt_in_tools() -> None:
    """A large prompt moved from messages into tool schemas must reserve the
    same cost — otherwise a caller undercharges by hiding weight from the
    estimator."""
    from routstr.payment.helpers import calculate_discounted_max_cost

    pricing = Mock()
    pricing.prompt = 0.5
    pricing.completion = 0.01
    pricing.max_prompt_cost = 100.0
    pricing.max_completion_cost = 100.0

    model_obj = Mock()
    model_obj.sats_pricing = pricing
    model_obj.top_provider = None
    model_obj.context_length = None

    big_text = "word " * 2_000
    base = {"model": "test-model", "max_tokens": 10}
    in_messages = {
        **base,
        "messages": [{"role": "user", "content": big_text}],
    }
    hiding_places = {
        "tools": {
            **base,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"type": "function", "function": {"name": "f", "description": big_text}}
            ],
        },
        # Anthropic forwards a top-level system prompt; it is billed like any other.
        "system": {
            **base,
            "messages": [{"role": "user", "content": "hi"}],
            "system": big_text,
        },
        # A key named like an image field must not win an image exclusion.
        "image-named key": {
            **base,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"function": {"parameters": {"data": big_text}}}],
        },
        # Nor may a caller-chosen "data:" prefix, in any field the body allows.
        "data-prefixed content": {
            **base,
            "messages": [{"role": "user", "content": "data:" + big_text}],
        },
        "data-prefixed text block": {
            **base,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "data:" + big_text}],
                }
            ],
        },
        "data-prefixed system": {
            **base,
            "messages": [{"role": "user", "content": "hi"}],
            "system": "data:" + big_text,
        },
    }

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
    ):
        cost_messages = await calculate_discounted_max_cost(
            150_000, in_messages, model_obj
        )
        for where, body in hiding_places.items():
            cost = await calculate_discounted_max_cost(150_000, body, model_obj)
            # Same prompt weight → at least the same reservation, never the floor.
            assert cost >= cost_messages, where
            assert cost > 1000, where


async def test_estimate_image_tokens_from_input_detail_and_file_id() -> None:
    import base64
    from io import BytesIO

    from PIL import Image

    from routstr.payment.helpers import estimate_image_tokens_from_input

    # file_id: dimensions can't be fetched, so use a conservative max-size
    # estimate (4 tiles for auto/high) and honor the detail sibling for low.
    assert await estimate_image_tokens_from_input(
        [{"type": "input_image", "file_id": "file-1"}]
    ) == 85 + (170 * 4)
    assert await estimate_image_tokens_from_input(
        [{"type": "input_image", "file_id": "file-1", "detail": "low"}]
    ) == 85

    # image_url honors the sibling detail instead of always defaulting to auto.
    image = Image.new("RGB", (512, 512), "red")
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

    assert await estimate_image_tokens_from_input(
        [{"type": "input_image", "image_url": data_url, "detail": "low"}]
    ) == 85
    assert await estimate_image_tokens_from_input(
        [{"type": "input_image", "image_url": data_url, "detail": "high"}]
    ) == 85 + 170  # 512x512 = 1 tile


def test_calculate_image_tokens_original_detail() -> None:
    from routstr.payment.helpers import _calculate_image_tokens

    # Patch-based pricing: ceil(patches * 1.2) tokens at 32x32px patches.
    assert _calculate_image_tokens(640, 640, "original") == 480  # 400 patches
    assert _calculate_image_tokens(2048, 2048, "original") == 4_916  # 4,096 patches
    # The same image on the tiled high-detail path caps at 765 tokens.
    assert _calculate_image_tokens(2048, 2048, "high") == 765
    # Above the 30,000-patch rejection limit the estimate is capped at
    # 36,000 tokens (30,000 patches * 1.2).
    assert _calculate_image_tokens(10_000, 10_000, "original") == 36_000


async def test_estimate_image_tokens_from_input_original_detail() -> None:
    import base64
    from io import BytesIO

    from PIL import Image

    from routstr.payment.helpers import estimate_image_tokens_from_input

    image = Image.new("RGB", (2048, 2048), "red")
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

    # image_url: billed at the decoded original resolution (4,096 patches),
    # not the 765-token tile cap.
    assert await estimate_image_tokens_from_input(
        [{"type": "input_image", "image_url": data_url, "detail": "original"}]
    ) == 4_916

    # file_id: dimensions unknown, so use the 30,000-patch worst case.
    assert await estimate_image_tokens_from_input(
        [{"type": "input_image", "file_id": "file-1", "detail": "original"}]
    ) == 36_000

    # Explicit null detail behaves like the auto default (tiled math).
    assert await estimate_image_tokens_from_input(
        [{"type": "input_image", "file_id": "file-1", "detail": None}]
    ) == 85 + (170 * 4)


async def test_estimate_image_tokens_in_messages_original_detail() -> None:
    """Chat Completions also accepts original detail via the nested dict."""
    import base64
    from io import BytesIO

    from PIL import Image

    from routstr.payment.helpers import estimate_image_tokens_in_messages

    image = Image.new("RGB", (640, 640), "blue")
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "original"},
                }
            ],
        }
    ]
    # 640x640 -> 20x20 = 400 patches -> ceil(400 * 1.2) = 480 tokens.
    assert await estimate_image_tokens_in_messages(messages) == 480

    # input_image parts inside messages honor their sibling detail and
    # file_id through the Responses estimator as well.
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "file_id": "file-1", "detail": "original"}
            ],
        }
    ]
    assert await estimate_image_tokens_in_messages(messages) == 36_000
