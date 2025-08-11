"""
Real Cashu mint integration for integration tests.

This module provides a real sixty_nuts Wallet implementation that can be used
with an actual Cashu mint instance for more thorough integration testing.
"""

import os
from typing import Optional, Tuple

from sixty_nuts import Wallet


class RealMintWallet:
    """Real Cashu mint wallet using sixty_nuts library"""

    def __init__(self, mint_url: str, nsec: str):
        self.mint_url = mint_url
        self.nsec = nsec
        self._wallet: Optional[Wallet] = None

    async def init(self) -> None:
        """Initialize the wallet connection"""
        if not self._wallet:
            self._wallet = await Wallet.create(nsec=self.nsec)

    @property
    def wallet(self) -> Wallet:
        """Get the wallet instance"""
        if not self._wallet:
            raise RuntimeError("Wallet not initialized. Call init() first.")
        return self._wallet

    async def redeem(self, cashu_token: str) -> Tuple[int, str]:
        """Redeem a Cashu token"""
        await self.init()
        return await self.wallet.redeem(cashu_token)

    async def send(self, amount: int) -> str:
        """Send amount as Cashu token"""
        await self.init()
        return await self.wallet.send(amount)

    async def send_to_lnurl(self, lnurl: str, amount: int) -> int:
        """Send to lightning address"""
        await self.init()
        return await self.wallet.send_to_lnurl(lnurl, amount)

    async def get_balance(self) -> int:
        """Get wallet balance"""
        await self.init()
        return await self.wallet.get_balance()


async def create_real_mint_wallet() -> RealMintWallet:
    """Create a real Cashu mint wallet for integration testing"""
    mint_url = os.environ.get(
        "MINT_URL", os.environ.get("MINT", "http://localhost:3338")
    )

    # Use a valid test nsec (this is a well-known test key)
    # In production, you would generate a unique key per test run
    test_nsec = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"

    wallet = RealMintWallet(mint_url=mint_url, nsec=test_nsec)
    await wallet.init()
    return wallet
