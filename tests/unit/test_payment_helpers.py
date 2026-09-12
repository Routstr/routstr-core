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


async def test_discounted_max_cost_counts_responses_input_images() -> None:
    import base64
    from io import BytesIO

    from PIL import Image

    from routstr.payment.helpers import calculate_discounted_max_cost

    pricing = Mock()
    pricing.prompt = 0.001
    pricing.completion = 0.001
    pricing.max_prompt_cost = 100.0
    pricing.max_completion_cost = 0.0

    model_obj = Mock()
    model_obj.sats_pricing = pricing
    model_obj.top_provider = None
    model_obj.context_length = None

    image = Image.new("RGB", (512, 512), "red")
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

    no_image = {
        "model": "test-model",
        "input": [{"role": "user", "content": "hi"}],
    }
    with_image = {
        "model": "test-model",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "hi"},
                    {"type": "input_image", "image_url": data_url, "detail": "high"},
                ],
            }
        ],
    }

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
    ):
        cost_no_image = await calculate_discounted_max_cost(
            100_000, no_image, model_obj
        )
        cost_with_image = await calculate_discounted_max_cost(
            100_000, with_image, model_obj
        )

    # The 512x512 high-detail image (85 + 170 = 255 tokens) is billed as prompt
    # weight, so it reserves strictly more than the identical text-only body.
    assert cost_with_image > cost_no_image


def _responses_image(url: str, detail: str | None = "original") -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [{"type": "input_image", "image_url": url, "detail": detail}],
        }
    ]


async def _responses_image_tokens(input_data: list[dict[str, Any]]) -> int:
    from routstr.payment.helpers import estimate_image_tokens_in_messages
    from routstr.payment.responses_input import responses_input_to_messages

    messages = responses_input_to_messages(input_data)
    assert messages is not None
    return await estimate_image_tokens_in_messages(messages)


async def test_remote_original_image_is_fetched_not_worst_cased() -> None:
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (512, 512), "red")
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    image_bytes = buffer.getvalue()

    with patch(
        "routstr.payment.helpers._fetch_image_from_url",
        new=AsyncMock(return_value=image_bytes),
    ):
        # 256 patches * 1.2
        assert (
            await _responses_image_tokens(_responses_image("https://x.test/i.jpg"))
            == 308
        )

    with patch(
        "routstr.payment.helpers._fetch_image_from_url",
        new=AsyncMock(return_value=None),
    ):
        assert (
            await _responses_image_tokens(_responses_image("https://x.test/i.jpg"))
            == 36_000
        )


async def test_broken_data_url_reserves_declared_detail_worst_case() -> None:
    from routstr.payment.helpers import estimate_image_tokens_in_messages

    broken = "data:image/jpeg;base64,!!!"
    assert await _responses_image_tokens(_responses_image(broken)) == 36_000
    assert await _responses_image_tokens(_responses_image(broken, "high")) == 85 + (
        170 * 4
    )

    chat = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": broken, "detail": "original"},
                }
            ],
        }
    ]
    assert await estimate_image_tokens_in_messages(chat) == 36_000


async def test_chat_original_image_fetch_failure_reserves_worst_case() -> None:
    from routstr.payment.helpers import estimate_image_tokens_in_messages

    chat = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "https://x.test/i.jpg", "detail": "original"},
                }
            ],
        }
    ]
    with patch(
        "routstr.payment.helpers._fetch_image_from_url",
        new=AsyncMock(return_value=None),
    ):
        assert await estimate_image_tokens_in_messages(chat) == 36_000


async def test_responses_images_share_per_request_fetch_cap() -> None:
    from routstr.payment.helpers import IMAGE_FETCH_MAX_PER_REQUEST

    input_data = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_image",
                    "image_url": f"https://x.test/{i}.jpg",
                    "detail": "original",
                }
                for i in range(IMAGE_FETCH_MAX_PER_REQUEST + 1)
            ],
        }
    ]
    fetch = AsyncMock(return_value=None)
    with patch("routstr.payment.helpers._fetch_image_from_url", new=fetch):
        tokens = await _responses_image_tokens(input_data)

    assert fetch.await_count == IMAGE_FETCH_MAX_PER_REQUEST
    assert tokens == 36_000 * (IMAGE_FETCH_MAX_PER_REQUEST + 1)


async def test_responses_transform_failure_falls_back_to_worst_case() -> None:
    from routstr.payment.helpers import calculate_discounted_max_cost

    pricing = Mock()
    pricing.prompt = 0.001
    pricing.completion = 0.001
    pricing.max_prompt_cost = 100.0
    pricing.max_completion_cost = 0.0

    model_obj = Mock()
    model_obj.sats_pricing = pricing
    model_obj.top_provider = None
    model_obj.context_length = None

    body = {
        "model": "test-model",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "https://x.test/a.jpg"},
                    {"type": "input_image", "image_url": "https://x.test/b.jpg"},
                ],
            }
        ],
    }
    fetch = AsyncMock(return_value=None)
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
        patch("routstr.payment.helpers._fetch_image_from_url", new=fetch),
        patch(
            "routstr.payment.responses_input.LiteLLMCompletionResponsesConfig."
            "transform_responses_api_input_to_messages",
            side_effect=RuntimeError("boom"),
        ),
    ):
        cost = await calculate_discounted_max_cost(100_000, body, model_obj)

    fetch.assert_not_awaited()
    # 2 * 36,000 tokens * 0.001 sats = 72 sats reserved
    assert 72_000 <= cost < 100_000


async def test_discounted_max_cost_body_max_output_tokens_fallback() -> None:
    """Body ``max_output_tokens`` (Responses API) is honored as a completion cap."""
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

    body = {"max_output_tokens": 80_000}

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
    ):
        cost = await calculate_discounted_max_cost(100_000, body, model_obj)

    assert cost == 80_000


async def test_discounted_max_cost_body_max_completion_tokens_fallback() -> None:
    """Body ``max_completion_tokens`` (modern chat) is honored as a completion cap."""
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


async def test_discounted_max_cost_uses_largest_completion_cap() -> None:
    """With several completion caps declared, the largest bounds the reservation.

    Upstream precedence between ``max_tokens`` / ``max_completion_tokens`` /
    ``max_output_tokens`` varies by provider, so reserving against anything
    but the largest could under-cover what the upstream bills.
    """
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

    body = {
        "max_tokens": 50_000,
        "max_completion_tokens": 10_000,
        "max_output_tokens": 80_000,
    }

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
    ):
        cost = await calculate_discounted_max_cost(100_000, body, model_obj)

    # 80_000 is the largest declared cap: 100.0 - 80.0 = 20 sats discount.
    assert cost == 80_000


async def test_discounted_max_cost_invalid_completion_cap_ignored() -> None:
    """Unparseable caps yield no completion discount rather than under-reserving."""
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

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1000),
    ):
        # No valid cap at all -> no completion discount.
        cost = await calculate_discounted_max_cost(
            100_000, {"max_completion_tokens": "sixty-four-k"}, model_obj
        )
        assert cost == 100_000

        # An invalid sibling does not poison a valid cap on another field.
        cost = await calculate_discounted_max_cost(
            100_000,
            {"max_tokens": "bad", "max_completion_tokens": 80_000},
            model_obj,
        )
        assert cost == 80_000


def _responses_file_image(detail: str | None) -> list[dict[str, Any]]:
    part: dict[str, Any] = {"type": "input_image", "file_id": "file-1"}
    if detail is not None:
        part["detail"] = detail
    return [{"role": "user", "content": [part]}]


async def test_responses_input_detail_and_file_id() -> None:
    import base64
    from io import BytesIO

    from PIL import Image

    # file_id: dimensions can't be fetched, so use a conservative max-size
    # estimate (4 tiles for auto/high) and honor the detail sibling for low.
    fetch = AsyncMock(return_value=None)
    with patch("routstr.payment.helpers._fetch_image_from_url", new=fetch):
        assert await _responses_image_tokens(_responses_file_image(None)) == 85 + (
            170 * 4
        )
        assert await _responses_image_tokens(_responses_file_image("low")) == 85
    fetch.assert_not_awaited()

    # image_url honors the sibling detail instead of always defaulting to auto.
    image = Image.new("RGB", (512, 512), "red")
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

    assert await _responses_image_tokens(_responses_image(data_url, "low")) == 85
    assert (
        await _responses_image_tokens(_responses_image(data_url, "high")) == 85 + 170
    )  # 512x512 = 1 tile


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


async def test_responses_input_original_detail() -> None:
    import base64
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (2048, 2048), "red")
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

    # image_url: billed at the decoded original resolution (4,096 patches),
    # not the 765-token tile cap.
    assert await _responses_image_tokens(_responses_image(data_url)) == 4_916

    # file_id: dimensions unknown, so use the 30,000-patch worst case.
    assert await _responses_image_tokens(_responses_file_image("original")) == 36_000

    # Explicit null detail behaves like the auto default (tiled math).
    assert await _responses_image_tokens(_responses_image(data_url, None)) == 85 + (
        170 * 4
    )


def test_responses_input_to_messages_shapes() -> None:
    from routstr.payment.responses_input import (
        FILE_ID_URL_PREFIX,
        count_input_images,
        responses_input_to_messages,
    )

    input_data = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "hi"},
                {"type": "input_image", "file_id": "file-1", "detail": "original"},
            ],
        },
        {"type": "function_call_output", "call_id": "c1", "output": "out"},
    ]
    messages = responses_input_to_messages(input_data)
    assert messages is not None
    assert messages[0]["role"] == "user"
    parts = messages[0]["content"]
    assert parts[0] == {"type": "text", "text": "hi"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"] == {
        "url": f"{FILE_ID_URL_PREFIX}file-1",
        "detail": "original",
    }
    assert messages[1]["role"] == "tool"

    # dict-form image_url: litellm nests it verbatim, so it is flattened first.
    nested = responses_input_to_messages(
        [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": {
                            "url": "https://x.test/a.jpg",
                            "detail": "original",
                        },
                    }
                ],
            }
        ]
    )
    assert nested is not None
    assert nested[0]["content"][0]["image_url"] == {
        "url": "https://x.test/a.jpg",
        "detail": "original",
    }

    assert responses_input_to_messages("plain") == [
        {"role": "user", "content": "plain"}
    ]
    assert responses_input_to_messages(None) == []
    assert count_input_images(input_data) == 1


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

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "file_id": "file-1", "detail": "original"}
            ],
        }
    ]
    assert await estimate_image_tokens_in_messages(messages) == 36_000
