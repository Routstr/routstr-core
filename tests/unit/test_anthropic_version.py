import httpx
import pytest

from routstr.upstream.anthropic import AnthropicUpstreamProvider


@pytest.mark.parametrize("workspace_id", [None, "wrk_test"])
def test_missing_version_defaults_to_stable_api_version(
    workspace_id: str | None,
) -> None:
    provider = AnthropicUpstreamProvider(api_key="sk-test", workspace_id=workspace_id)
    headers = provider.prepare_headers({"content-type": "application/json"})

    assert headers["anthropic-version"] == "2023-06-01"
    assert headers.get("anthropic-workspace-id") == workspace_id


@pytest.mark.parametrize(
    "header_name", ["anthropic-version", "Anthropic-Version", "ANTHROPIC-VERSION"]
)
@pytest.mark.parametrize("version", ["2099-01-01", ""])
def test_explicit_version_is_preserved_case_insensitively(
    header_name: str, version: str
) -> None:
    provider = AnthropicUpstreamProvider(api_key="sk-test")
    # An unsupported explicit version must still reach Anthropic for validation.
    headers = provider.prepare_headers({header_name: version})
    version_headers = {
        name: value
        for name, value in headers.items()
        if name.lower() == "anthropic-version"
    }

    assert version_headers == {header_name: version}


def test_beta_header_is_not_a_substitute_for_version() -> None:
    provider = AnthropicUpstreamProvider(api_key="sk-test")
    headers = provider.prepare_headers({"anthropic-beta": "test-beta"})

    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "test-beta"


@pytest.mark.asyncio
async def test_default_version_reaches_messages_transport() -> None:
    provider = AnthropicUpstreamProvider(api_key="sk-test", workspace_id="wrk_test")

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["anthropic-workspace-id"] == "wrk_test"
        return httpx.Response(200, json={"type": "message"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        response = await client.post(
            f"{provider.base_url}/messages",
            headers=provider.prepare_headers({"content-type": "application/json"}),
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Reply OK."}],
            },
        )

    assert response.status_code == 200
