import os
import asyncio
import time
import logging

from sixty_nuts import Wallet
from sqlmodel import select, func, col
from .db import ApiKey, AsyncSession, get_session

logger = logging.getLogger(__name__)


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
        logger.info("Initiating payout")
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
                logger.warning(
                    "Wallet balance (%s sats) is less than user balance (%s sats). Skipping payout.",
                    wallet_balance_sats,
                    user_balance_sats,
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
            logger.info(
                "Payout sent: owners %s sats, devs %s sats", owners_draw, devs_donation
            )

    except Exception as e:
        # Log the error but don't crash - payouts can be retried later
        logger.error("Error in pay_out: %s", e)


async def credit_balance(cashu_token: str, key: ApiKey, session: AsyncSession) -> int:
    logger.info("Redeeming token for key %s...", key.hashed_key[:10])
    state_before = await WALLET.fetch_wallet_state()
    await WALLET.redeem(cashu_token)
    state_after = await WALLET.fetch_wallet_state()
    amount = (state_after.balance - state_before.balance) * 1000
    key.balance += amount
    session.add(key)
    await session.commit()
    logger.info("Credited %s msats to key %s", amount, key.hashed_key[:10])
    return amount


async def check_for_refunds() -> None:
    """
    Periodically checks for API keys that are eligible for refunds and processes them.

    Raises:
        Exception: If an error occurs during the refund check process.
    """
    # Setting REFUND_PROCESSING_INTERVAL to 0 disables it
    if REFUND_PROCESSING_INTERVAL == 0:
        logger.info("Automatic refund processing is disabled.")
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
                        logger.info(
                            "       DEBUG   Refunding key %s, Current Time: %s, Expirary Time: %s",
                            key.hashed_key[:3] + "[...]" + key.hashed_key[-3:],
                            current_time,
                            key.key_expiry_time,
                        )
                        await refund_balance(key.balance, key, session)

            # Sleep for the specified interval before checking again
            await asyncio.sleep(REFUND_PROCESSING_INTERVAL)
        except Exception as e:
            logger.error("Error during refund check: %s", e)


async def refund_balance(amount_msats: int, key: ApiKey, session: AsyncSession) -> int:
    if key.balance < amount_msats:
        raise ValueError("Insufficient balance.")
    if amount_msats <= 0:
        amount_msats = key.balance

    # Convert msats to sats for cashu wallet
    amount_sats = amount_msats // 1000
    if amount_sats == 0:
        raise ValueError("Amount too small to refund (less than 1 sat)")

    key.balance -= amount_msats
    session.add(key)
    await session.commit()
    logger.info(
        "Refunding %s msats from key %s to %s",
        amount_msats,
        key.hashed_key[:10],
        key.refund_address,
    )

    if key.refund_address is None:
        raise ValueError("Refund address not set.")

    result = await WALLET.send_to_lnurl(
        key.refund_address,
        amount=amount_sats,
    )
    logger.info("Refund sent: %s", result)
    return result


async def redeem(cashu_token: str, lnurl: str) -> int:
    logger.info("Redeeming token for lnurl %s", lnurl)
    state_before = await WALLET.fetch_wallet_state()
    await WALLET.redeem(cashu_token)
    state_after = await WALLET.fetch_wallet_state()
    amount = state_after.balance - state_before.balance
    await WALLET.send_to_lnurl(lnurl, amount=amount)
    logger.info("Redeemed %s sats", amount)
    return amount
