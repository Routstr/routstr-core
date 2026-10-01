from __future__ import annotations

import os

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

import pytest  # noqa: E402

from routstr.proxy import _forwarding_allowed  # noqa: E402
from routstr.upstream.base import (  # noqa: E402
    BaseUpstreamProvider,
    _x_cashu_path_has_settlement_handler,
)
from routstr.upstream.openai import OpenAIUpstreamProvider  # noqa: E402
from routstr.upstream.openrouter import OpenRouterUpstreamProvider  # noqa: E402


@pytest.mark.parametrize("path", ["v1/decisions", "decisions", "v1/decisions/"])
def test_decisions_is_forwarded(path: str) -> None:
    assert _forwarding_allowed(path, "POST") is True


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("v1/decisions", "GET"),
        ("v1/decisionsdump", "POST"),
        ("v1/decisions/dec_123", "POST"),
        ("v1/alpha/decisions", "POST"),
        ("v1/decisions/../admin", "POST"),
    ],
)
def test_decisions_lookalikes_are_refused(path: str, method: str) -> None:
    assert _forwarding_allowed(path, method) is False


def test_decisions_has_x_cashu_settlement_handler() -> None:
    assert _x_cashu_path_has_settlement_handler("v1/decisions") is True
    assert _x_cashu_path_has_settlement_handler("decisions/") is True


def test_only_openai_supports_decisions() -> None:
    assert OpenAIUpstreamProvider.supports_decisions is True
    assert BaseUpstreamProvider.supports_decisions is False
    assert OpenRouterUpstreamProvider.supports_decisions is False
