"""Beta-gated body fields get their Anthropic beta header when a relay drops it."""

import json

import pytest

from routstr.upstream.anthropic import AnthropicUpstreamProvider
from routstr.upstream.generic import GenericUpstreamProvider


def betas(headers: dict) -> list[str]:
    values = [v for k, v in headers.items() if k.lower() == "anthropic-beta"]
    assert len(values) <= 1
    return values[0].split(",") if values else []


def prepare(body: object, headers: dict | None = None) -> dict:
    provider = AnthropicUpstreamProvider(api_key="test")
    raw = json.dumps(body).encode() if body is not None else None
    return provider.prepare_body_headers(dict(headers or {}), raw)


@pytest.mark.parametrize(
    ("body", "beta"),
    [
        ({"context_management": {"edits": []}}, "context-management-2025-06-27"),
        ({"safeguards": {}}, "dangerous-tool-use-2026-09-03"),
        (
            {"messages": [{"role": "user", "content": "hi", "output_config": {}}]},
            "per-turn-control-2026-07-01",
        ),
        (
            {"thinking": {"type": "adaptive", "display": "updates"}},
            "thinking-display-updates-2026-08-18",
        ),
    ],
)
def test_missing_beta_is_added(body: dict, beta: str) -> None:
    assert betas(prepare(body)) == [beta]


def test_all_claude_code_fields_add_all_betas() -> None:
    body = {
        "context_management": {"edits": []},
        "safeguards": {},
        "thinking": {"type": "adaptive", "display": "updates"},
        "output_config": {"effort": "high"},
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": "go", "output_config": {"effort": "low"}},
        ],
    }
    assert betas(prepare(body)) == [
        "context-management-2025-06-27",
        "dangerous-tool-use-2026-09-03",
        "thinking-display-updates-2026-08-18",
        "per-turn-control-2026-07-01",
    ]


def test_client_betas_are_kept_and_not_duplicated() -> None:
    headers = prepare(
        {"context_management": {}, "safeguards": {}},
        {"Anthropic-Beta": "claude-code-20250219,context-management-2025-06-27"},
    )
    assert headers == {
        "Anthropic-Beta": "claude-code-20250219,context-management-2025-06-27,"
        "dangerous-tool-use-2026-09-03"
    }


@pytest.mark.parametrize(
    "body",
    [
        {"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "hi"}]},
        {"output_config": {"effort": "high"}, "messages": []},
        {"messages": "not-a-list", "output_config": {}},
        [1, 2],
        None,
    ],
)
def test_headers_unchanged_without_beta_fields(body: object) -> None:
    headers = {"anthropic-version": "2023-06-01"}
    assert prepare(body, headers) == headers


def test_unparseable_body_leaves_headers_alone() -> None:
    provider = AnthropicUpstreamProvider(api_key="test")
    headers = {"anthropic-version": "2023-06-01"}
    body = b'{"context_management": \xff'
    assert provider.prepare_body_headers(dict(headers), body) == headers


def test_other_providers_do_not_add_betas() -> None:
    provider = GenericUpstreamProvider(base_url="https://example.com/v1", api_key="k")
    body = json.dumps({"context_management": {}}).encode()
    assert provider.prepare_body_headers({}, body) == {}
