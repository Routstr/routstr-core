"""Client credentials for the node must never be forwarded to an upstream."""

import pytest

from routstr.upstream.anthropic import AnthropicUpstreamProvider
from routstr.upstream.azure import AzureUpstreamProvider
from routstr.upstream.base import BaseUpstreamProvider

CLIENT_KEY = "sk-client-routstr-key"


@pytest.mark.parametrize("name", ["x-api-key", "X-Api-Key", "api-key", "Api-Key"])
@pytest.mark.parametrize(
    "provider",
    [
        BaseUpstreamProvider("https://api.test.com", "sk-upstream"),
        AnthropicUpstreamProvider(api_key="sk-upstream"),
    ],
)
def test_client_api_key_headers_are_stripped(
    provider: BaseUpstreamProvider, name: str
) -> None:
    headers = provider.prepare_headers({name: CLIENT_KEY, "x-custom": "kept"})

    assert CLIENT_KEY not in headers.values()
    assert headers["Authorization"] == "Bearer sk-upstream"
    assert headers["x-custom"] == "kept"


def test_azure_sends_its_own_api_key_not_the_clients() -> None:
    provider = AzureUpstreamProvider(
        base_url="https://example.openai.azure.com",
        api_key="azure-upstream",
        api_version="2024-10-21",
    )
    headers = provider.prepare_headers({"api-key": CLIENT_KEY})

    assert headers["api-key"] == "azure-upstream"
    assert CLIENT_KEY not in headers.values()
