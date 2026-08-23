"""Unit tests for the proxy edge path allowlist (arbitrary-upstream-path-proxy).

An authenticated POST used to be forwarded for ANY path, so a caller could reach
arbitrary or traversal-shaped upstream endpoints with the provider credential
attached. The proxy now rejects ambiguous path spellings for every method and
requires a known API prefix before anything is forwarded.
"""

from __future__ import annotations

import os

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

import pytest  # noqa: E402

from routstr.proxy import (  # noqa: E402
    _forwarding_allowed,
    _is_ambiguously_spelled_path,
)


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "v1/../admin",
        "v1/./models",
        "..",
        "v1//models",  # duplicate separator
        "/v1/models",  # leading slash / absolute override
        "v1/models/..",
        "%2e%2e/secret",  # residual encoded dot segment
        "v1/%2fadmin",  # residual encoded slash
        "v1\\models",  # backslash
        "v1/models\x00",  # NUL byte
        " v1/models",  # leading whitespace
        "",
    ],
)
def test_ambiguous_paths_are_rejected(path: str) -> None:
    assert _is_ambiguously_spelled_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "v1/chat/completions",
        "chat/completions",
        "v1/responses",
        "v1/embeddings",
        "models",
        "v1/models/gpt-4",
        "attestation/",  # a single trailing slash is canonical
        "tee/attestation/",
    ],
)
def test_canonical_paths_are_allowed(path: str) -> None:
    assert _is_ambiguously_spelled_path(path) is False


def test_unknown_paths_are_not_forwarded() -> None:
    # The credential is attached during forwarding, so an unknown endpoint must
    # never be forwarded on the caller's say-so.
    assert _forwarding_allowed("internal/admin", is_ehbp=False) is False
    assert _forwarding_allowed("secret-endpoint", is_ehbp=False) is False


@pytest.mark.parametrize(
    "path",
    [
        "modelsdump",  # bare token "models" must not match a longer segment
        "attestationadmin",
        "providers-secret",
        "embeddingsx",
        "completions-internal",
    ],
)
def test_bare_prefix_does_not_match_a_longer_segment(path: str) -> None:
    assert _forwarding_allowed(path, is_ehbp=False) is False


def test_known_prefixes_are_forwarded() -> None:
    assert _forwarding_allowed("v1/chat/completions", is_ehbp=False) is True
    assert _forwarding_allowed("chat/completions", is_ehbp=False) is True
    # Bare tokens match a whole segment: exactly or followed by "/".
    assert _forwarding_allowed("models", is_ehbp=False) is True
    assert _forwarding_allowed("models/gpt-4", is_ehbp=False) is True
    assert _forwarding_allowed("embeddings", is_ehbp=False) is True
    assert _forwarding_allowed("attestation", is_ehbp=False) is True


def test_ehbp_bypasses_prefix_gate_by_header() -> None:
    # Documents a deliberate exemption: EHBP is identified by header and carries
    # its own encrypted contract, so the prefix gate does not apply. The
    # ambiguous-spelling screen still runs on EHBP paths (see the ordering test
    # in the integration suite).
    assert _forwarding_allowed("anything/encrypted", is_ehbp=True) is True
