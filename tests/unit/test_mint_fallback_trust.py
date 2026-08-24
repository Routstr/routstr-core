"""Persisted mint preferences must not bypass the configured trusted set."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from routstr.core.settings import settings
from routstr.lightning import _request_mint_with_fallback

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


async def test_mint_quote_timeout_is_not_retried() -> None:
    wallet = MagicMock()
    wallet.request_mint = AsyncMock(side_effect=httpx.ReadTimeout("response lost"))

    with (
        patch.object(settings, "primary_mint", TRUSTED),
        patch.object(settings, "cashu_mints", [TRUSTED]),
        patch.object(settings, "mint_retry_max_attempts", 3),
        patch("routstr.lightning.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.lightning.mint_cooldown_remaining", return_value=0.0),
        pytest.raises(Exception),
    ):
        await _request_mint_with_fallback(10)

    wallet.request_mint.assert_awaited_once_with(10)


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
