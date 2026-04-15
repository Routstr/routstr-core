import asyncio
import time
from typing import TypedDict

from cashu.core.base import Proof, Token
from cashu.wallet.helpers import deserialize_token_from_string
from cashu.wallet.wallet import Wallet
from sqlmodel import col, select, update

from .core import db, get_logger
from .core.settings import settings
from .payment.lnurl import raw_send_to_lnurl

logger = get_logger(__name__)


async def get_balance(unit: str) -> int:
    wallet = await get_wallet(settings.primary_mint, unit)
    return wallet.available_balance.amount


async def recieve_token(
    token: str,
) -> tuple[int, str, str]:  # amount, unit, mint_url
    token_obj = deserialize_token_from_string(token)
    if len(token_obj.keysets) > 1:
        raise ValueError("Multiple keysets per token currently not supported")

    wallet = await get_wallet(token_obj.mint, token_obj.unit, load=False)
    wallet.keyset_id = token_obj.keysets[0]

    if token_obj.mint not in settings.cashu_mints:
        return await swap_to_primary_mint(token_obj, wallet)

    wallet.verify_proofs_dleq(token_obj.proofs)
    await wallet.split(proofs=token_obj.proofs, amount=0, include_fees=True)

    return token_obj.amount, token_obj.unit, token_obj.mint


async def send(amount: int, unit: str, mint_url: str | None = None) -> tuple[int, str]:
    """Internal send function - returns amount and serialized token"""
    wallet: Wallet = await get_wallet(mint_url or settings.primary_mint, unit)
    proofs = get_proofs_per_mint_and_unit(
        wallet, mint_url or settings.primary_mint, unit
    )

    send_proofs, _ = await wallet.select_to_send(
        proofs, amount, set_reserved=True, include_fees=False
    )
    token = await wallet.serialize_proofs(
        send_proofs, include_dleq=False, legacy=False, memo=None
    )
    return amount, token


async def send_token(amount: int, unit: str, mint_url: str | None = None) -> str:
    _, token = await send(amount, unit, mint_url)
    return token


async def _calculate_swap_amount(
    amount_msat: int,
    token_unit: str,
    token_mint_url: str,
    token_wallet: Wallet,
    primary_wallet: Wallet,
) -> int:
    """
    Calculate the amount to mint on the primary mint after accounting for
    potential swap fees (melt fees) on the foreign mint.
    """
    if settings.primary_mint_unit == "sat":
        receive_amount = amount_msat // 1000
    else:
        receive_amount = amount_msat

    if token_mint_url == settings.primary_mint:
        logger.info(
            "swap_to_primary_mint: skipping fee estimation (same mint)",
            extra={"minted_amount": receive_amount},
        )
        return int(receive_amount)

    logger.info(
        "swap_to_primary_mint: estimating fees",
        extra={
            "dummy_amount": receive_amount,
            "unit": settings.primary_mint_unit,
        },
    )

    try:
        dummy_mint_quote = await primary_wallet.request_mint(receive_amount)
        dummy_melt_quote = await token_wallet.melt_quote(dummy_mint_quote.request)

        fee_reserve = dummy_melt_quote.fee_reserve
        if token_unit == "sat":
            fee_msat = fee_reserve * 1000
        else:
            fee_msat = fee_reserve

        amount_msat_after_fee = amount_msat - fee_msat

        if settings.primary_mint_unit == "sat":
            minted_amount = int(amount_msat_after_fee // 1000)
        else:
            minted_amount = int(amount_msat_after_fee)

        if minted_amount <= 0:
            raise ValueError(f"Fees ({fee_reserve} {token_unit}) exceed token amount")

        logger.info(
            "swap_to_primary_mint: fee estimation result",
            extra={
                "token_amount_sat": amount_msat // 1000,
                "estimated_fee_sat": fee_msat // 1000,
                "minted_amount": minted_amount,
                "minted_unit": settings.primary_mint_unit,
            },
        )
        return minted_amount

    except Exception as e:
        logger.error(
            "swap_to_primary_mint: fee estimation failed",
            extra={"error": str(e)},
        )
        raise ValueError(f"Failed to estimate fees: {e}") from e


async def swap_to_primary_mint(
    token_obj: Token, token_wallet: Wallet
) -> tuple[int, str, str]:
    logger.info(
        "swap_to_primary_mint: starting",
        extra={
            "foreign_mint": token_obj.mint,
            "token_amount": token_obj.amount,
            "unit": token_obj.unit,
            "primary_mint": settings.primary_mint,
        },
    )
    # Ensure amount is an integer
    if not isinstance(token_obj.amount, int):
        token_amount = int(token_obj.amount)
    else:
        token_amount = token_obj.amount

    if token_obj.unit == "sat":
        amount_msat = token_amount * 1000
    elif token_obj.unit == "msat":
        amount_msat = token_amount
    else:
        raise ValueError("Invalid unit")
    primary_wallet = await get_wallet(settings.primary_mint, settings.primary_mint_unit)

    # If the token is already from the primary mint, we don't need to swap
    # and we definitely don't want to calculate or pay fees.
    if token_obj.mint == settings.primary_mint:
        logger.info(
            "swap_to_primary_mint: token already on primary mint, skipping swap",
            extra={
                "mint": token_obj.mint,
                "amount": token_amount,
                "unit": token_obj.unit,
            },
        )
        await token_wallet.split(proofs=token_obj.proofs, amount=0, include_fees=True)
        return token_amount, token_obj.unit, token_obj.mint

    minted_amount = await _calculate_swap_amount(
        amount_msat,
        token_obj.unit,
        token_obj.mint,
        token_wallet,
        primary_wallet,
    )

    mint_quote = await primary_wallet.request_mint(minted_amount)
    logger.info(
        "swap_to_primary_mint: mint quote received",
        extra={"mint_quote_id": mint_quote.quote},
    )

    melt_quote = await token_wallet.melt_quote(mint_quote.request)
    total_needed = melt_quote.amount + melt_quote.fee_reserve
    logger.info(
        "swap_to_primary_mint: melt quote received",
        extra={
            "melt_quote_id": melt_quote.quote,
            "melt_amount": melt_quote.amount,
            "melt_fee_reserve": melt_quote.fee_reserve,
            "total_needed": total_needed,
            "token_amount": token_amount,
        },
    )

    if total_needed > token_amount:
        logger.warning(
            "swap_to_primary_mint: insufficient token amount for melt fees",
            extra={
                "token_amount": token_amount,
                "melt_amount": melt_quote.amount,
                "melt_fee_reserve": melt_quote.fee_reserve,
                "total_needed": total_needed,
                "shortfall": total_needed - token_amount,
            },
        )
        raise ValueError(
            f"Token amount ({token_amount} {token_obj.unit}) is insufficient to cover "
            f"melt fees. Needed: {total_needed} {token_obj.unit} "
            f"(amount: {melt_quote.amount} + fee: {melt_quote.fee_reserve})"
        )

    try:
        _ = await token_wallet.melt(
            proofs=token_obj.proofs,
            invoice=mint_quote.request,
            fee_reserve_sat=melt_quote.fee_reserve,
            quote_id=melt_quote.quote,
        )
    except Exception as e:
        logger.error(
            "swap_to_primary_mint: melt failed",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "foreign_mint": token_obj.mint,
                "token_amount": token_amount,
                "melt_quote_id": melt_quote.quote,
                "total_needed": total_needed,
            },
        )
        raise ValueError(
            f"Failed to melt token from foreign mint {token_obj.mint}: {e}"
        ) from e

    logger.info(
        "swap_to_primary_mint: melt succeeded, minting on primary",
        extra={"minted_amount": minted_amount, "mint_quote_id": mint_quote.quote},
    )

    try:
        _ = await primary_wallet.mint(minted_amount, quote_id=mint_quote.quote)
    except Exception as e:
        logger.error(
            "swap_to_primary_mint: mint on primary failed after successful melt",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "minted_amount": minted_amount,
                "mint_quote_id": mint_quote.quote,
            },
        )
        raise

    logger.info(
        "swap_to_primary_mint: completed successfully",
        extra={
            "foreign_mint": token_obj.mint,
            "primary_mint": settings.primary_mint,
            "original_amount": token_amount,
            "minted_amount": minted_amount,
            "unit": settings.primary_mint_unit,
        },
    )

    return int(minted_amount), settings.primary_mint_unit, settings.primary_mint


async def credit_balance(
    cashu_token: str, key: db.ApiKey, session: db.AsyncSession
) -> int:
    logger.info(
        "credit_balance: Starting token redemption",
        extra={"token_preview": cashu_token[:50]},
    )

    try:
        amount, unit, mint_url = await recieve_token(cashu_token)
        logger.info(
            "credit_balance: Token redeemed successfully",
            extra={"amount": amount, "unit": unit, "mint_url": mint_url},
        )

        if unit == "sat":
            amount = amount * 1000
            logger.info(
                "credit_balance: Converted to msat", extra={"amount_msat": amount}
            )

        logger.info(
            "credit_balance: Updating balance",
            extra={"old_balance": key.balance, "credit_amount": amount},
        )

        # Use atomic SQL UPDATE to prevent race conditions during concurrent topups
        stmt = (
            update(db.ApiKey)
            .where(col(db.ApiKey.hashed_key) == key.hashed_key)
            .values(balance=(db.ApiKey.balance) + amount)
        )
        await session.exec(stmt)  # type: ignore[call-overload]
        await session.commit()
        await session.refresh(key)

        logger.info(
            "credit_balance: Balance updated successfully",
            extra={"new_balance": key.balance},
        )

        logger.info(
            "Cashu token successfully redeemed and stored",
            extra={"amount": amount, "unit": unit, "mint_url": mint_url},
        )
        return amount
    except Exception as e:
        logger.error(
            "credit_balance: Error during token redemption",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise


_wallets: dict[str, Wallet] = {}


async def get_wallet(mint_url: str, unit: str = "sat", load: bool = True) -> Wallet:
    global _wallets
    id = f"{mint_url}_{unit}"
    if id not in _wallets:
        _wallets[id] = await Wallet.with_db(mint_url, db=".wallet", unit=unit)

    if load:
        await _wallets[id].load_mint()
        await _wallets[id].load_proofs(reload=True)
    return _wallets[id]


def get_proofs_per_mint_and_unit(
    wallet: Wallet, mint_url: str, unit: str, not_reserved: bool = False
) -> list[Proof]:
    valid_keyset_ids = [
        k.id
        for k in wallet.keysets.values()
        if k.mint_url == mint_url and k.unit.name == unit
    ]
    proofs = [p for p in wallet.proofs if p.id in valid_keyset_ids]
    if not_reserved:
        proofs = [p for p in proofs if not p.reserved]
    return proofs


async def slow_filter_spend_proofs(proofs: list[Proof], wallet: Wallet) -> list[Proof]:
    if not proofs:
        return []
    _proofs = []
    _spent_proofs = []
    for i in range(0, len(proofs), 1000):
        pb = proofs[i : i + 1000]
        proof_states = await wallet.check_proof_state(pb)
        for proof, state in zip(pb, proof_states.states):
            if str(state.state) != "spent":
                _proofs.append(proof)
            else:
                _spent_proofs.append(proof)
    await wallet.set_reserved_for_send(_spent_proofs, reserved=True)
    return _proofs


class BalanceDetail(TypedDict, total=False):
    mint_url: str
    unit: str
    wallet_balance: int
    user_balance: int
    owner_balance: int
    error: str


async def fetch_all_balances(
    units: list[str] | None = None,
) -> tuple[list[BalanceDetail], int, int, int]:
    """
    Fetch balances for all trusted mints and units concurrently.

    Returns:
        - List of balance details for each mint/unit combination
        - Total wallet balance in sats
        - Total user balance in sats
        - Owner balance in sats (wallet - user)
    """
    if units is None:
        units = ["sat", "msat"]

    async def fetch_balance(
        session: db.AsyncSession, mint_url: str, unit: str
    ) -> BalanceDetail:
        try:
            wallet = await get_wallet(mint_url, unit)
            proofs = get_proofs_per_mint_and_unit(
                wallet, mint_url, unit, not_reserved=True
            )
            proofs = await slow_filter_spend_proofs(proofs, wallet)
            user_balance = await db.balances_for_mint_and_unit(session, mint_url, unit)
            if unit == "sat":
                user_balance = user_balance // 1000
            proofs_balance = sum(proof.amount for proof in proofs)

            result: BalanceDetail = {
                "mint_url": mint_url,
                "unit": unit,
                "wallet_balance": proofs_balance,
                "user_balance": user_balance,
                "owner_balance": proofs_balance - user_balance,
            }
            return result
        except Exception as e:
            logger.error(f"Error getting balance for {mint_url} {unit}: {e}")
            error_result: BalanceDetail = {
                "mint_url": mint_url,
                "unit": unit,
                "wallet_balance": 0,
                "user_balance": 0,
                "owner_balance": 0,
                "error": str(e),
            }
            return error_result

    # Create tasks for all mint/unit combinations
    async with db.create_session() as session:
        tasks = [
            fetch_balance(session, mint_url, unit)
            for mint_url in settings.cashu_mints
            for unit in units
        ]

        # Run all tasks concurrently
        balance_details = list(await asyncio.gather(*tasks))

    # Calculate totals
    total_wallet_balance_sats = 0
    total_user_balance_sats = 0

    for detail in balance_details:
        if not detail.get("error"):
            # Convert to sats for total calculation
            unit = detail["unit"]
            proofs_balance_sats = (
                detail["wallet_balance"]
                if unit == "sat"
                else detail["wallet_balance"] // 1000
            )
            user_balance_sats = (
                detail["user_balance"]
                if unit == "sat"
                else detail["user_balance"] // 1000
            )

            total_wallet_balance_sats += proofs_balance_sats
            total_user_balance_sats += user_balance_sats

    owner_balance = total_wallet_balance_sats - total_user_balance_sats

    return (
        balance_details,
        total_wallet_balance_sats,
        total_user_balance_sats,
        owner_balance,
    )


async def periodic_payout() -> None:
    if not settings.receive_ln_address:
        logger.error("RECEIVE_LN_ADDRESS is not set, skipping payout")
        return
    while True:
        await asyncio.sleep(60 * 15)
        try:
            async with db.create_session() as session:
                for mint_url in settings.cashu_mints:
                    for unit in ["sat", "msat"]:
                        wallet = await get_wallet(mint_url, unit)
                        proofs = get_proofs_per_mint_and_unit(
                            wallet, mint_url, unit, not_reserved=True
                        )
                        proofs = await slow_filter_spend_proofs(proofs, wallet)
                        await asyncio.sleep(5)
                        user_balance = await db.balances_for_mint_and_unit(
                            session, mint_url, unit
                        )
                        if unit == "sat":
                            user_balance = user_balance // 1000
                        proofs_balance = sum(proof.amount for proof in proofs)
                        available_balance = proofs_balance - user_balance
                        min_amount = 210 if unit == "sat" else 210000
                        if available_balance > min_amount:
                            amount_received = await raw_send_to_lnurl(
                                wallet,
                                proofs,
                                settings.receive_ln_address,
                                unit,
                                amount=available_balance,
                            )
                            logger.info(
                                "Payout sent successfully",
                                extra={
                                    "mint_url": mint_url,
                                    "unit": unit,
                                    "balance": available_balance,
                                    "amount_received": amount_received,
                                },
                            )
        except Exception as e:
            logger.error(
                f"Error sending payout: {type(e).__name__}",
                extra={"error": str(e)},
            )


async def periodic_refund_sweep() -> None:
    while True:
        await asyncio.sleep(60 * 60)  # every hour
        try:
            cutoff = int(time.time()) - settings.refund_sweep_ttl_seconds
            async with db.create_session() as session:
                stmt = select(db.CashuTransaction).where(
                    db.CashuTransaction.type == "out",
                    db.CashuTransaction.collected == False,  # noqa: E712
                    db.CashuTransaction.swept == False,  # noqa: E712
                    db.CashuTransaction.created_at < cutoff,
                )
                results = await session.exec(stmt)
                refunds = results.all()

                for refund in refunds:
                    try:
                        await recieve_token(refund.token)
                        refund.swept = True
                        session.add(refund)
                        logger.info(
                            "Swept uncollected refund",
                            extra={
                                "id": refund.id,
                                "amount": refund.amount,
                                "unit": refund.unit,
                            },
                        )
                    except Exception as e:
                        error_msg = str(e).lower()
                        if "already spent" in error_msg:
                            refund.collected = True
                            session.add(refund)
                            logger.info(
                                "Refund already spent (client collected), marking swept",
                                extra={
                                    "id": refund.id,
                                },
                            )
                        else:
                            logger.warning(
                                "Failed to sweep refund",
                                extra={
                                    "id": refund.id,
                                    "error": str(e),
                                },
                            )
                await session.commit()
        except Exception as e:
            logger.error(
                "Error in periodic refund sweep",
                extra={"error": str(e), "error_type": type(e).__name__},
            )


async def periodic_routstr_fee_payout() -> None:
    from .auth import (
        ROUTSTR_FEE_DEFAULT_PAYOUT,
        ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS,
        ROUTSTR_LN_ADDRESS,
    )

    if not ROUTSTR_LN_ADDRESS:
        logger.info("ROUTSTR_LN_ADDRESS not set, skipping fee payout")
        return
    while True:
        await asyncio.sleep(ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS)
        try:
            async with db.create_session() as session:
                fee = await db.get_routstr_fee(session)
                accumulated_sats = fee.accumulated_msats // 1000
                if accumulated_sats >= ROUTSTR_FEE_DEFAULT_PAYOUT:
                    wallet = await get_wallet(settings.primary_mint, "sat")
                    proofs = get_proofs_per_mint_and_unit(
                        wallet, settings.primary_mint, "sat", not_reserved=True
                    )
                    amount_received = await raw_send_to_lnurl(
                        wallet, proofs, ROUTSTR_LN_ADDRESS, "sat", amount=accumulated_sats
                    )
                    paid_msats = accumulated_sats * 1000
                    await db.reset_routstr_fee(session, paid_msats)
                    logger.info(
                        "Routstr fee payout sent",
                        extra={
                            "accumulated_sats": accumulated_sats,
                            "amount_received": amount_received,
                        },
                    )
        except Exception as e:
            logger.error(
                f"Error in Routstr fee payout: {type(e).__name__}",
                extra={"error": str(e)},
            )


async def send_to_lnurl(amount: int, unit: str, mint: str, address: str) -> int:
    wallet = await get_wallet(mint, unit)
    proofs = wallet._get_proofs_per_keyset(wallet.proofs)[wallet.keyset_id]
    proofs, _ = await wallet.select_to_send(proofs, amount, set_reserved=True)
    return await raw_send_to_lnurl(wallet, proofs, address, unit)


# class Payment:
#     """
#     Stores all cashu payment related data
#     """

#     def __init__(self, token: str) -> None:
#         self.initial_token = token
#         amount, unit, mint_url = self.parse_token(token)
#         self.amount = amount
#         self.unit = unit
#         self.mint_url = mint_url

#         self.claimed_proofs = redeem_to_proofs(token)

#     def parse_token(self, token: str) -> tuple[int, CurrencyUnit, str]:
#         raise NotImplementedError

#     def refund_full(self) -> None:
#         raise NotImplementedError

#     def refund_partial(self, amount: int) -> None:
#         raise NotImplementedError
