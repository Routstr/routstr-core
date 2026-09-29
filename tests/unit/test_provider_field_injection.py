from routstr.upstream.anthropic import AnthropicUpstreamProvider
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.generic import GenericUpstreamProvider
from routstr.upstream.openrouter import OpenRouterUpstreamProvider


def _make_provider(cls: type, provider_type: str) -> BaseUpstreamProvider:
    p = cls(api_key="test_key")
    assert p.provider_type == provider_type
    return p


def test_apply_provider_field_direct_upstream() -> None:
    """For a direct upstream (no upstream-reported provider), the field
    is just the provider_type string."""
    p = _make_provider(AnthropicUpstreamProvider, "anthropic")
    data: dict = {"id": "msg_1", "model": "claude-3-5-sonnet"}
    p._apply_provider_field(data)
    assert data["provider"] == "anthropic"


def test_apply_provider_field_openrouter_passthrough() -> None:
    """OpenRouter responses include an upstream ``provider`` string —
    routstr should prefix with its own provider_type."""
    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    data: dict = {
        "id": "gen-abc",
        "model": "anthropic/claude-3.5-sonnet",
        "provider": "Anthropic",
    }
    p._apply_provider_field(data)
    assert data["provider"] == "openrouter:Anthropic"


def test_apply_provider_field_openrouter_no_upstream_provider() -> None:
    """If OpenRouter omits the provider field, the real serving provider is
    unknown — a bare ``openrouter`` value carries no information."""
    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    data: dict = {"id": "gen-abc"}
    p._apply_provider_field(data)
    assert data["provider"] == "unknown"


def test_apply_provider_field_openrouter_echoes_router_name() -> None:
    """If OpenRouter reports its own name as the provider, treat as unknown."""
    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    data: dict = {"provider": "openrouter"}
    p._apply_provider_field(data)
    assert data["provider"] == "unknown"


def test_apply_provider_field_openrouter_idempotent_no_double_prefix() -> None:
    """Re-stamping must never nest the prefix: openrouter only once."""
    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    data: dict = {"provider": "GMICloud"}
    p._apply_provider_field(data)
    assert data["provider"] == "openrouter:GMICloud"
    # Second pass (e.g. streaming) keeps a single prefix.
    p._apply_provider_field(data)
    assert data["provider"] == "openrouter:GMICloud"


def test_apply_provider_field_openrouter_collapses_existing_double_prefix() -> None:
    """A pre-existing double prefix is collapsed to a single one."""
    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    data: dict = {"provider": "openrouter:openrouter:GMICloud"}
    p._apply_provider_field(data)
    assert data["provider"] == "openrouter:GMICloud"


def test_apply_provider_field_strips_whitespace() -> None:
    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    data: dict = {"provider": "  Fireworks  "}
    p._apply_provider_field(data)
    assert data["provider"] == "openrouter:Fireworks"


def test_apply_provider_field_blank_upstream_treated_as_missing() -> None:
    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    data: dict = {"provider": "   "}
    p._apply_provider_field(data)
    assert data["provider"] == "unknown"


def test_apply_provider_field_non_string_upstream_treated_as_missing() -> None:
    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    data: dict = {"provider": 42}
    p._apply_provider_field(data)
    assert data["provider"] == "unknown"


def test_apply_provider_field_openrouter_reads_nested_envelopes() -> None:
    """Anthropic ``message`` and Responses ``response`` envelopes nest the
    upstream provider; it must not be reported as unknown."""
    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    message_start: dict = {
        "type": "message_start",
        "message": {"provider": "Anthropic"},
    }
    p._apply_provider_field(message_start)
    assert message_start["provider"] == "openrouter:Anthropic"

    created: dict = {"type": "response.created", "response": {"provider": "OpenAI"}}
    p._apply_provider_field(created)
    assert created["provider"] == "openrouter:OpenAI"


def test_stamp_streamed_provider_carries_earlier_provider() -> None:
    """Events without their own provider inherit the one reported earlier in
    the stream instead of becoming ``unknown``."""
    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    first: dict = {"provider": "Fireworks"}
    carried = p._stamp_streamed_provider(first, None)
    delta: dict = {"type": "content_block_delta"}
    assert p._stamp_streamed_provider(delta, carried) == "Fireworks"
    assert first["provider"] == delta["provider"] == "openrouter:Fireworks"


def test_apply_provider_field_idempotent_for_direct_upstream() -> None:
    """Calling twice on a direct upstream payload keeps the same value and
    never nests the prefix (no ``anthropic:anthropic``)."""
    p = _make_provider(AnthropicUpstreamProvider, "anthropic")
    data: dict = {}
    p._apply_provider_field(data)
    p._apply_provider_field(data)
    assert data["provider"] == "anthropic"


def test_apply_provider_field_ignores_non_dict() -> None:
    """Lists / primitives must be skipped silently."""
    p = _make_provider(AnthropicUpstreamProvider, "anthropic")
    # Should not raise.
    p._apply_provider_field([1, 2, 3])  # type: ignore[arg-type]
    p._apply_provider_field("hello")  # type: ignore[arg-type]
    p._apply_provider_field(None)  # type: ignore[arg-type]


def test_inject_cost_metadata_sets_provider() -> None:
    """``inject_cost_metadata`` is the unified injection point and must
    also stamp the provider field."""
    from unittest.mock import MagicMock

    from routstr.core.db import ApiKey

    p = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    key = MagicMock(spec=ApiKey)
    key.balance = 1000

    response_json: dict = {
        "model": "anthropic/claude-3.5-sonnet",
        "provider": "Anthropic",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    cost_data = {"total_msats": 2500, "total_usd": 0.0025}
    p.inject_cost_metadata(response_json, cost_data, key)

    assert response_json["provider"] == "openrouter:Anthropic"


def test_apply_provider_field_generic_uses_upstream_host() -> None:
    """A generic upstream has no router-reported provider; the serving host
    identifies it, mirroring ``openrouter:<sub-provider>``."""
    p = GenericUpstreamProvider(base_url="https://api.deepseek.com/v1", api_key="k")
    data: dict = {"id": "chatcmpl-1", "model": "deepseek-chat"}
    p._apply_provider_field(data)
    assert data["provider"] == "generic:api.deepseek.com"


def test_apply_provider_field_generic_keeps_upstream_reported_provider() -> None:
    p = GenericUpstreamProvider(base_url="https://api.deepseek.com/v1", api_key="k")
    data: dict = {"provider": "Fireworks"}
    p._apply_provider_field(data)
    assert data["provider"] == "generic:Fireworks"


def test_apply_provider_field_generic_idempotent() -> None:
    p = GenericUpstreamProvider(base_url="https://api.deepseek.com/v1", api_key="k")
    data: dict = {}
    p._apply_provider_field(data)
    p._apply_provider_field(data)
    assert data["provider"] == "generic:api.deepseek.com"


def test_apply_provider_field_sets_provider_url() -> None:
    """Every provider exposes the upstream base URL it served from."""
    generic = GenericUpstreamProvider(
        base_url="https://api.deepseek.com/v1", api_key="k"
    )
    data: dict = {}
    generic._apply_provider_field(data)
    assert data["provider_url"] == "https://api.deepseek.com/v1"

    openrouter = _make_provider(OpenRouterUpstreamProvider, "openrouter")
    data = {"provider": "Anthropic"}
    openrouter._apply_provider_field(data)
    assert data["provider_url"] == "https://openrouter.ai/api/v1"


def test_apply_provider_field_masks_private_upstream() -> None:
    """Private or port-bearing upstream URLs are masked the same way model
    paths mask them, so neither ``provider`` nor ``provider_url`` leaks a
    local address."""
    p = GenericUpstreamProvider(base_url="http://10.0.0.5:11434/v1", api_key="k")
    data: dict = {}
    p._apply_provider_field(data)
    assert data["provider"] == "generic:localhost"
    assert data["provider_url"] == "http://localhost"
