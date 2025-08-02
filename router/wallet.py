import os
from typing import Literal

from cashu.core.base import Token, Unit
from cashu.wallet.helpers import deserialize_token_from_string, receive, send
from cashu.wallet.wallet import Wallet

from .core import db, get_logger

logger = get_logger(__name__)

CurrencyUnit = Literal["sat", "msat"]

TRUSTED_MINTS = os.environ["CASHU_MINTS"].split(",")
PRIMARY_MINT_URL = TRUSTED_MINTS[0]


async def get_balance(unit: CurrencyUnit) -> int:
    wallet = await Wallet.with_db(
        PRIMARY_MINT_URL,
        # DATABASE_URL,
        db=".wallet",
        load_all_keysets=True,
        unit=unit,
    )
    await wallet.load_proofs()
    return wallet.available_balance.amount


async def recieve_token(
    token: str,
) -> tuple[int, CurrencyUnit, str]:  # amount, unit, mint_url
    # trusted_mints = os.environ["CASHU_MINTS"].split(",")
    token_obj = deserialize_token_from_string(token)
    wallet = await Wallet.with_db(
        token_obj.mint,
        db=".wallet",
        load_all_keysets=True,
        unit=token_obj.unit,
    )

    if token_obj.mint != PRIMARY_MINT_URL:
        raise ValueError(
            f"This mint is not supported, please use {PRIMARY_MINT_URL} instead"
        )

    await receive(wallet, token_obj)
    return token_obj.amount, token_obj.unit, token_obj.mint


async def send_token(
    amount: int, unit: CurrencyUnit, mint_url: str | None = None
) -> str:
    wallet = await Wallet.with_db(
        mint_url or PRIMARY_MINT_URL,
        db=".wallet",
        load_all_keysets=True,
        unit=unit,
    )
    balance, token = await send(wallet, amount=amount, lock="", legacy=False)
    return token


async def swap_to_primary_mint(
    token_obj: Token, token_wallet: Wallet
) -> tuple[int, CurrencyUnit, str]:
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
    estimated_fee_sat = max(amount_msat // 1000 * 0.01, 2)
    amount_msat_after_fee = amount_msat - estimated_fee_sat * 1000
    print(f"amount_msat_after_fee: {amount_msat_after_fee}")
    primary_wallet = await Wallet.with_db(
        PRIMARY_MINT_URL, db=".temp", load_all_keysets=True, unit="sat"
    )
    await primary_wallet.load_mint_keysets()
    mint_quote = await primary_wallet.mint_quote(
        amount_msat_after_fee // 1000, Unit.sat
    )
    print(f"mint_quote: {mint_quote}")
    melt_quote = await token_wallet.melt_quote(mint_quote.request)
    print(f"melt_quote: {melt_quote}")
    melt_quote_resp = await token_wallet.melt(
        proofs=token_obj.proofs,
        invoice=mint_quote.request,
        fee_reserve_sat=melt_quote.fee_reserve,
        quote_id=melt_quote.quote,
    )
    print(f"melt_quote_resp: {melt_quote_resp}")

    _ = await primary_wallet.mint(amount_msat_after_fee // 1000, mint_quote.quote)

    return amount_msat_after_fee // 1000, "sat", PRIMARY_MINT_URL


async def credit_balance(
    cashu_token: str, key: db.ApiKey, session: db.AsyncSession
) -> int:
    amount, unit, mint_url = await recieve_token(cashu_token)
    if unit == "sat":
        amount = amount * 1000
    if mint_url != PRIMARY_MINT_URL:
        raise ValueError("Mint URL is not supported by this proxy")
    key.balance += amount
    session.add(key)
    await session.commit()
    logger.info(
        "Cashu token successfully redeemed and stored",
        extra={"amount": amount, "unit": unit, "mint_url": mint_url},
    )
    return amount


async def send_to_lnurl(amount: int, unit: CurrencyUnit, lnurl: str) -> dict[str, int]:
    raise NotImplementedError


async def check_for_refunds() -> None:
    logger.warning("check_for_refunds, temporary not implemented")


async def periodic_payout() -> None:
    logger.warning("periodic_payout, temporary not implemented")


# class Proof:
#     """
#     Represents an ecash bill
#     """


# def redeem_to_proofs(self, token: str) -> list[Proof]:
#     raise NotImplementedError


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
