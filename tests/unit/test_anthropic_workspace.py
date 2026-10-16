import json

import pytest

from routstr.core.db import UpstreamProviderRow
from routstr.upstream.anthropic import AnthropicUpstreamProvider


@pytest.mark.parametrize("workspace_id", [None, "", "   ", "wrk_test"])
def test_workspace_header_from_provider_settings(workspace_id: str | None) -> None:
    row = UpstreamProviderRow(
        provider_type="anthropic",
        base_url=AnthropicUpstreamProvider.default_base_url,
        api_key="sk-test",
        provider_settings=json.dumps({"workspace_id": workspace_id}),
    )
    provider = AnthropicUpstreamProvider._build_from_row(row)
    headers = provider.prepare_headers({"content-type": "application/json"})

    if workspace_id and workspace_id.strip():
        assert headers["anthropic-workspace-id"] == workspace_id
    else:
        assert "anthropic-workspace-id" not in headers
    assert headers["Authorization"] == "Bearer sk-test"


@pytest.mark.parametrize(
    "settings", [None, "{}", "null", "[]", "invalid", '{"workspace_id": 123}']
)
def test_provider_without_valid_workspace_settings(settings: str | None) -> None:
    row = UpstreamProviderRow(
        provider_type="anthropic",
        base_url=AnthropicUpstreamProvider.default_base_url,
        api_key="sk-test",
        provider_settings=settings,
    )
    provider = AnthropicUpstreamProvider._build_from_row(row)
    assert "anthropic-workspace-id" not in provider.prepare_headers({})


def test_configured_workspace_overrides_client_header_case_insensitively() -> None:
    provider = AnthropicUpstreamProvider(api_key="sk-test", workspace_id=" wrk_test ")
    headers = provider.prepare_headers(
        {"Anthropic-Workspace-Id": "wrk_client", "anthropic-workspace-id": "wrk_other"}
    )
    workspace_headers = {
        name: value
        for name, value in headers.items()
        if name.lower() == "anthropic-workspace-id"
    }
    assert workspace_headers == {"anthropic-workspace-id": "wrk_test"}
