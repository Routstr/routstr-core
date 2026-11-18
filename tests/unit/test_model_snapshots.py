from routstr.upstream.anthropic import AnthropicUpstreamProvider
from routstr.upstream.model_snapshots import provider_config_hash


def test_provider_config_hash_canonicalizes_nested_settings() -> None:
    first: dict[str, object] = {
        "image_prices": {"image-a": {"usd": 0.02, "unit": "image"}, "b": 0.01}
    }
    reordered: dict[str, object] = {
        "image_prices": {"b": 0.01, "image-a": {"unit": "image", "usd": 0.02}}
    }
    assert provider_config_hash("together", "url", "key", first) == provider_config_hash(
        "together", "url", "key", reordered
    )


def test_anthropic_workspace_is_part_of_catalog_fingerprint() -> None:
    first = AnthropicUpstreamProvider(api_key="key", workspace_id="first")
    second = AnthropicUpstreamProvider(api_key="key", workspace_id="second")
    assert provider_config_hash(
        first.provider_type, first.base_url, first.api_key, first._catalog_settings()
    ) != provider_config_hash(
        second.provider_type, second.base_url, second.api_key, second._catalog_settings()
    )
