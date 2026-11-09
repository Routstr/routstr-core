import pytest

from routstr.core.settings import settings
from routstr.upstream import usage_multiplier_sources


@pytest.fixture(autouse=True)
def _disable_usage_multiplier_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep catalog refreshes off OpenRouter; multiplier tests opt back in."""
    monkeypatch.setattr(settings, "usage_multiplier_source", "none")
    monkeypatch.setattr(usage_multiplier_sources, "_instances", {})
