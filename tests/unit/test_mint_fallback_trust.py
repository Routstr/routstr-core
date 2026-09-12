"""Persisted mint preferences must not bypass the configured trusted set."""

from unittest.mock import AsyncMock, patch

import pytest

from routstr.core.settings import settings
from routstr.lightning import _request_mint_with_fallback
from routstr.mint import MintCooldownError, is_mint_rate_limited

TRUSTED = "https://good-mint.example.com"
UNTRUSTED = "https://removed-mint.example.com"


async def test_untrusted_allowed_mints_fall_back_to_trusted_set() -> None:
    attempted: list[str] = []

    async def fake_get_wallet(mint_url: str, unit: str, **kwargs: object) -> None:
        attempted.append(mint_url)
        raise ConnectionError("unreachable in test")

    with (
        patch.object(settings, "primary_mint", TRUSTED),
        patch.object(settings, "cashu_mints", [TRUSTED]),
        patch("routstr.lightning.get_wallet", AsyncMock(side_effect=fake_get_wallet)),
        patch("routstr.lightning.mint_cooldown_remaining", return_value=0.0),
    ):
        with pytest.raises(Exception):
            await _request_mint_with_fallback(10, allowed_mints=[UNTRUSTED])

    assert UNTRUSTED not in attempted
    assert attempted == [TRUSTED]


async def test_trusted_allowed_mints_are_used_verbatim() -> None:
    attempted: list[str] = []

    async def fake_get_wallet(mint_url: str, unit: str, **kwargs: object) -> None:
        attempted.append(mint_url)
        raise ConnectionError("unreachable in test")

    with (
        patch.object(settings, "primary_mint", TRUSTED),
        patch.object(settings, "cashu_mints", [TRUSTED, "https://other.example.com"]),
        patch("routstr.lightning.get_wallet", AsyncMock(side_effect=fake_get_wallet)),
        patch("routstr.lightning.mint_cooldown_remaining", return_value=0.0),
    ):
        with pytest.raises(Exception):
            await _request_mint_with_fallback(10, allowed_mints=[TRUSTED])

    assert attempted == [TRUSTED]


async def test_all_cooling_down_mints_preserve_rate_limit_error() -> None:
    with (
        patch.object(settings, "primary_mint", TRUSTED),
        patch.object(settings, "cashu_mints", [TRUSTED]),
        patch("routstr.lightning.mint_cooldown_remaining", return_value=30.0),
    ):
        with pytest.raises(MintCooldownError) as caught:
            await _request_mint_with_fallback(10)

    assert is_mint_rate_limited(caught.value)
