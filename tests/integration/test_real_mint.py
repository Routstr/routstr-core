"""
Script to test real Cashu mint integration.
Run this with USE_REAL_MINT=true after starting a Cashu mint instance.
"""

import asyncio
import os

try:
    from .real_testmint import create_real_mint_wallet
except ImportError:
    # sixty_nuts not available, tests will be skipped
    create_real_mint_wallet = None  # type: ignore


async def test_real_wallet() -> None:
    """Test basic operations with a real Cashu mint wallet"""
    print("Testing real Cashu mint wallet...")

    # Check if sixty_nuts dependency is available
    if create_real_mint_wallet is None:
        print("sixty_nuts not available. Skipping real mint tests.")
        return

    # Check if real mint is enabled
    if os.environ.get("USE_REAL_MINT", "false").lower() != "true":
        print("USE_REAL_MINT is not set to true. Set it to test real Cashu mint.")
        return

    try:
        # Create wallet
        wallet = await create_real_mint_wallet()
        print(f"Created wallet connected to: {wallet.mint_url}")

        # Get balance
        balance = await wallet.get_balance()
        print(f"Wallet balance: {balance} sats")

        # Test send operation (create a token)
        if balance > 100:
            token = await wallet.send(100)
            print("Created token for 100 sats")
            print(f"  Token: {token[:50]}...")

            # Test redeem operation
            amount, metadata = await wallet.redeem(token)
            print(f"Redeemed token: {amount} sats")
        else:
            print("WARNING: Insufficient balance to test send/redeem operations")

        print("\nReal Cashu mint integration is working!")

    except Exception as e:
        print(f"\nError testing real Cashu mint: {e}")
        print("\nMake sure:")
        print("1. Cashu mint is running (use ./setup_cashu_mint.sh)")
        print("2. MINT_URL is set correctly")
        print("3. The mint has some balance for testing")


if __name__ == "__main__":
    asyncio.run(test_real_wallet())
