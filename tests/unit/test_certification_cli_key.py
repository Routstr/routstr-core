"""The certification CLI reads the upstream key from the environment."""

from __future__ import annotations

from typing import Any

import pytest

from routstr.upstream import certification


@pytest.fixture
def captured_keys(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    keys: list[str] = []

    async def fake_certify(url: str, *, api_key: str, **_: Any) -> dict[str, Any]:
        keys.append(api_key)
        return {"url": url, "rows": []}

    monkeypatch.setattr(certification, "certify_upstream_url", fake_certify)
    monkeypatch.setattr(certification, "render_checklist", lambda _result: "")
    return keys


def test_key_defaults_to_env_var(
    monkeypatch: pytest.MonkeyPatch, captured_keys: list[str]
) -> None:
    monkeypatch.setenv("ROUTSTR_CERTIFY_KEY", "sk-from-env")
    assert certification.main(["--url", "http://localhost:1/v1"]) == 0
    assert captured_keys == ["sk-from-env"]


def test_key_flag_overrides_env_var(
    monkeypatch: pytest.MonkeyPatch, captured_keys: list[str]
) -> None:
    monkeypatch.setenv("ROUTSTR_CERTIFY_KEY", "sk-from-env")
    certification.main(["--url", "http://localhost:1/v1", "--key", "sk-flag"])
    assert captured_keys == ["sk-flag"]


def test_key_is_empty_without_flag_or_env(
    monkeypatch: pytest.MonkeyPatch, captured_keys: list[str]
) -> None:
    monkeypatch.delenv("ROUTSTR_CERTIFY_KEY", raising=False)
    certification.main(["--url", "http://localhost:1/v1"])
    assert captured_keys == [""]
