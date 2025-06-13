import os
import asyncio
import time

from sixty_nuts import Wallet
from sqlmodel import select, func, col, update
from .db import ApiKey, AsyncSession, get_session


RECEIVE_LN_ADDRESS = os.environ["RECEIVE_LN_ADDRESS"]
MINT = os.environ.get("MINT", "https://mint.minibits.cash/Bitcoin")
MINIMUM_PAYOUT = int(os.environ.get("MINIMUM_PAYOUT", 100))
REFUND_PROCESSING_INTERVAL = int(os.environ.get("REFUND_PROCESSING_INTERVAL", 3600))
DEV_LN_ADDRESS = "routstr@minibits.cash"
DEVS_DONATION_RATE = float(os.environ.get("DEVS_DONATION_RATE", 0.021))  # 2.1%
NSEC = os.environ["NSEC"]  # Nostr private key for the wallet

WALLET = Wallet(nsec=NSEC, mint_urls=[MINT])


async def init_wallet():
    global WALLET
    WALLET = await Wallet.create(nsec=NSEC, mint_urls=[MINT])


async def close_wallet():
    global WALLET
    await WALLET.aclose()


async def pay_out() -> None:
    """
    Calculates the pay-out amount based on the spent balance, profit, and donation rate.
    """
    try:
        from .db import create_session

        async with create_session() as session:
            balance = (
                await session.exec(
                    select(func.sum(col(ApiKey.balance))).where(ApiKey.balance > 0)
                )
            ).one()
            if balance is None or balance == 0:
                # No balance to pay out - this is OK, not an error
                return

            user_balance_sats = balance // 1000
            state = await WALLET.fetch_wallet_state()
            wallet_balance_sats = state.balance

            # Handle edge cases more gracefully
            if wallet_balance_sats < user_balance_sats:
                print(
                    f"Warning: Wallet balance ({wallet_balance_sats} sats) is less than user balance ({user_balance_sats} sats). Skipping payout."
                )
                return

            if (revenue := wallet_balance_sats - user_balance_sats) <= MINIMUM_PAYOUT:
                # Not enough revenue yet - this is OK
                return

            devs_donation = int(revenue * DEVS_DONATION_RATE)
            owners_draw = revenue - devs_donation

            # Send payouts
            await WALLET.send_to_lnurl(RECEIVE_LN_ADDRESS, owners_draw)
            await WALLET.send_to_lnurl(DEV_LN_ADDRESS, devs_donation)

    except Exception as e:
        # Log the error but don't crash - payouts can be retried later
        print(f"Error in pay_out: {e}")


async def credit_balance(cashu_token: str, key: ApiKey, session: AsyncSession) -> int:
    """Redeem a Cashu token and credit the amount to the API key balance."""
    try:
        amount_sats = await WALLET.redeem(cashu_token)
    except Exception:
        # Ensure the balance cannot become negative if redeem fails
        return 0

    if amount_sats <= 0:
        return 0

    amount_msats = amount_sats * 1000
    key.balance += amount_msats

    session.add(key)
    await session.flush()

    # Apply the balance change atomically to avoid race conditions when topping
    # up the same key concurrently.
    stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == key.hashed_key)
        .values(balance=col(ApiKey.balance) + amount_msats)
    )
    await session.exec(stmt)  # type: ignore[call-overload]
    await session.commit()

    return amount_msats


async def check_for_refunds() -> None:
    """
    Periodically checks for API keys that are eligible for refunds and processes them.

    Raises:
        Exception: If an error occurs during the refund check process.
    """
    # Setting REFUND_PROCESSING_INTERVAL to 0 disables it
    if REFUND_PROCESSING_INTERVAL == 0:
        print("Automatic refund processing is disabled.")
        return

    while True:
        try:
            async for session in get_session():
                result = await session.exec(select(ApiKey))
                keys = result.all()
                current_time = int(time.time())
                for key in keys:
                    if (
                        key.balance > 0
                        and key.refund_address
                        and key.key_expiry_time
                        and key.key_expiry_time < current_time
                    ):
                        print(
                            f"       DEBUG   Refunding key {key.hashed_key[:3] + '[...]' + key.hashed_key[-3:]}, Current Time: {current_time}, Expirary Time: {key.key_expiry_time}",
                            flush=True,
                        )
                        await refund_balance(key.balance, key, session)

            # Sleep for the specified interval before checking again
            await asyncio.sleep(REFUND_PROCESSING_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Error during refund check: {e}")


async def refund_balance(amount_msats: int, key: ApiKey, session: AsyncSession) -> int:
    if amount_msats <= 0:
        amount_msats = key.balance

    # Convert msats to sats for cashu wallet
    amount_sats = amount_msats // 1000
    if amount_sats == 0:
        raise ValueError("Amount too small to refund (less than 1 sat)")

    # Atomically deduct the balance to avoid race conditions when multiple
    # refunds are triggered concurrently.
    stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == key.hashed_key)
        .where(col(ApiKey.balance) >= amount_msats)
        .values(balance=col(ApiKey.balance) - amount_msats)
    )
    result = await session.exec(stmt)  # type: ignore[call-overload]
    await session.commit()
    if result.rowcount == 0:
        raise ValueError("Insufficient balance.")
    await session.refresh(key)

    if key.refund_address is None:
        raise ValueError("Refund address not set.")

    return await WALLET.send_to_lnurl(
        key.refund_address,
        amount=amount_sats,
    )


async def redeem(cashu_token: str, lnurl: str) -> int:
    state_before = await WALLET.fetch_wallet_state()
    await WALLET.redeem(cashu_token)
    state_after = await WALLET.fetch_wallet_state()
    amount = state_after.balance - state_before.balance
    await WALLET.send_to_lnurl(lnurl, amount=amount)
    return amount
