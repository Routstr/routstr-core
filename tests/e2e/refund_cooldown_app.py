"""Test node: put the real mint selector on cooldown only after redemption."""

from routstr.core.main import app as app
from routstr.mint import MintRateGuard
from routstr.upstream import base
from routstr.wallet import send_token


async def send_during_cooldown(
    amount: int, unit: str, mint_url: str | None = None
) -> str:
    assert mint_url is not None
    MintRateGuard.get(mint_url).apply_cooldown(300, reason="rate_limited")
    return await send_token(amount, unit, mint_url)


base.send_token = send_during_cooldown
