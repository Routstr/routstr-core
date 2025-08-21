import asyncio
import os
from typing import TypedDict

from cashu.core.base import Proof, Token
from cashu.wallet.helpers import deserialize_token_from_string
from cashu.wallet.wallet import Wallet

from .core import db, get_logger
from .payment.lnurl import raw_send_to_lnurl

logger = get_logger(__name__)


CASHU_MINTS = os.environ.get("CASHU_MINTS", "https://mint.minibits.cash/Bitcoin")
TRUSTED_MINTS = CASHU_MINTS.split(",")
PRIMARY_MINT_URL = TRUSTED_MINTS[0]
RECEIVE_LN_ADDRESS = os.environ.get("RECEIVE_LN_ADDRESS", "")


async def get_balance(unit: str) -> int:
    wallet = await get_wallet(PRIMARY_MINT_URL, unit)
    return wallet.available_balance.amount


async def recieve_token(
    token: str,
) -> tuple[int, str, str]:  # amount, unit, mint_url
    token_obj = deserialize_token_from_string(token)
    if len(token_obj.keysets) > 1:
        raise ValueError("Multiple keysets per token currently not supported")

    wallet = await get_wallet(token_obj.mint, token_obj.unit, load=False)
    wallet.keyset_id = token_obj.keysets[0]

    if token_obj.mint not in TRUSTED_MINTS:
        return await swap_to_primary_mint(token_obj, wallet)

    wallet.verify_proofs_dleq(token_obj.proofs)
    await wallet.split(proofs=token_obj.proofs, amount=0, include_fees=True)
    return token_obj.amount, token_obj.unit, token_obj.mint


async def send(amount: int, unit: str, mint_url: str | None = None) -> tuple[int, str]:
    """Internal send function - returns amount and serialized token"""
    wallet: Wallet = await get_wallet(mint_url or PRIMARY_MINT_URL, unit)
    proofs = await get_proofs_per_mint_and_unit(
        wallet, mint_url or PRIMARY_MINT_URL, unit
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


async def swap_to_primary_mint(
    token_obj: Token, token_wallet: Wallet
) -> tuple[int, str, str]:
    logger.info(
        "swap_to_primary_mint",
        extra={
            "mint": token_obj.mint,
            "amount": token_obj.amount,
            "unit": token_obj.unit,
        },
    )
    if token_obj.unit == "sat":
        amount_msat = token_obj.amount * 1000
    elif token_obj.unit == "msat":
        amount_msat = token_obj.amount
    else:
        raise ValueError("Invalid unit")
    estimated_fee_sat = int(max(amount_msat // 1000 * 0.01, 2))
    amount_msat_after_fee = amount_msat - estimated_fee_sat * 1000
    primary_wallet = await get_wallet(PRIMARY_MINT_URL, "sat")

    minted_amount = amount_msat_after_fee // 1000
    mint_quote = await primary_wallet.request_mint(minted_amount)

    melt_quote = await token_wallet.melt_quote(mint_quote.request)
    _ = await token_wallet.melt(
        proofs=token_obj.proofs,
        invoice=mint_quote.request,
        fee_reserve_sat=melt_quote.fee_reserve,
        quote_id=melt_quote.quote,
    )
    _ = await primary_wallet.mint(minted_amount, quote_id=mint_quote.quote)

    return minted_amount, "sat", PRIMARY_MINT_URL


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
        key.balance += amount
        session.add(key)
        await session.commit()
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
        _wallets[id] = await Wallet.with_db(
            mint_url, db=".wallet", load_all_keysets=True, unit=unit
        )

    if load:
        await _wallets[id].load_mint()
        await _wallets[id].load_proofs(reload=True)
    return _wallets[id]


async def get_proofs_per_mint_and_unit(
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
            proofs = await get_proofs_per_mint_and_unit(
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
            for mint_url in TRUSTED_MINTS
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
    if not RECEIVE_LN_ADDRESS:
        logger.error("RECEIVE_LN_ADDRESS is not set, skipping payout")
        return
    while True:
        await asyncio.sleep(60 * 5)
        try:
            async with db.create_session() as session:
                for mint_url in TRUSTED_MINTS:
                    for unit in ["sat", "msat"]:
                        wallet = await get_wallet(mint_url, unit)
                        proofs = await get_proofs_per_mint_and_unit(
                            wallet, mint_url, unit, not_reserved=True
                        )
                        proofs = await slow_filter_spend_proofs(proofs, wallet)
                        user_balance = await db.balances_for_mint_and_unit(
                            session, mint_url, unit
                        )
                        if unit == "sat":
                            user_balance = user_balance // 1000
                        proofs_balance = sum(proof.amount for proof in proofs)
                        available_balance = proofs_balance - user_balance
                        print(f"Balance: {proofs_balance} {unit}")
                        print(f"User balance: {user_balance} {unit}")
                        print(f"Available balance: {available_balance} {unit}")
                        min_amount = 210 if unit == "sat" else 210000
                        if proofs_balance > min_amount:
                            amount_received = await raw_send_to_lnurl(
                                wallet, proofs, RECEIVE_LN_ADDRESS, unit
                            )
                            print(f"Amount received: {amount_received}")
                            logger.info(
                                "Payout sent successfully",
                                extra={
                                    "mint_url": mint_url,
                                    "unit": unit,
                                    "balance": proofs_balance,
                                },
                            )
                        else:
                            logger.info(
                                "Not enough balance to send payout",
                                extra={
                                    "mint_url": mint_url,
                                    "unit": unit,
                                    "balance": proofs_balance,
                                },
                            )
                        await asyncio.sleep(5)
        except Exception as e:
            logger.error(
                f"Error sending payout: {type(e).__name__}",
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
