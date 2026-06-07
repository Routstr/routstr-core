from routstr.upstream.anthropic import AnthropicUpstreamProvider
from routstr.upstream.base import BaseUpstreamProvider
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
