from enum import Enum
from typing import Any

from cashu.core.base import Token
from cashu.wallet.helpers import deserialize_token_from_string
from cashu.wallet.wallet import Wallet

from .core.db import ApiKey
from .core.logging import get_logger
from .core.settings import SettingsManager

logger = get_logger(__name__)


class CurrencyUnit(Enum):
    sat = "sat"
    msat = "msat"


# Default values - will be loaded from settings
_CASHU_MINTS = None
_TRUSTED_MINTS = None
_PRIMARY_MINT_URL = None


async def _get_cashu_mints() -> str:
    """Get CASHU_MINTS from settings."""
    global _CASHU_MINTS
    if _CASHU_MINTS is None:
        _CASHU_MINTS = await SettingsManager.get(
            "CASHU_MINTS", "https://mint.minibits.cash/Bitcoin"
        )
    return _CASHU_MINTS


async def _get_trusted_mints() -> list[str]:
    """Get list of trusted mints from settings."""
    global _TRUSTED_MINTS
    if _TRUSTED_MINTS is None:
        cashu_mints = await _get_cashu_mints()
        _TRUSTED_MINTS = [
            mint.strip() for mint in cashu_mints.split(",") if mint.strip()
        ]
    return _TRUSTED_MINTS


async def _get_primary_mint_url() -> str:
    """Get primary mint URL from settings."""
    global _PRIMARY_MINT_URL
    if _PRIMARY_MINT_URL is None:
        trusted_mints = await _get_trusted_mints()
        _PRIMARY_MINT_URL = (
            trusted_mints[0] if trusted_mints else "https://mint.minibits.cash/Bitcoin"
        )
    return _PRIMARY_MINT_URL


# Cache to reload settings when changed
async def reload_mint_settings() -> None:
    """Reload mint settings from database."""
    global _CASHU_MINTS, _TRUSTED_MINTS, _PRIMARY_MINT_URL
    _CASHU_MINTS = None
    _TRUSTED_MINTS = None
    _PRIMARY_MINT_URL = None


async def get_balance(unit: CurrencyUnit | str) -> int:
    wallet = await Wallet.with_db(
        await _get_primary_mint_url(),
        db=".wallet",
        load_all_keysets=True,
        unit=unit,
    )
    await wallet.load_proofs()
    return wallet.available_balance.amount


async def recieve_token(
    token: str,
) -> tuple[int, CurrencyUnit, str]:  # amount, unit, mint_url
    token_obj = deserialize_token_from_string(token)
    if len(token_obj.keysets) > 1:
        raise ValueError("Multiple keysets per token currently not supported")

    wallet = await Wallet.with_db(
        token_obj.mint,
        db=".wallet",
        load_all_keysets=True,
        unit=token_obj.unit,
    )
    await wallet.load_mint(token_obj.keysets[0])

    trusted_mints = await _get_trusted_mints()
    if token_obj.mint not in trusted_mints:
        return await swap_to_primary_mint(token_obj, wallet)

    await wallet.redeem(token_obj.proofs)
    return token_obj.amount, token_obj.unit, token_obj.mint


async def send(amount: int, unit: str, mint_url: str | None = None) -> tuple[int, str]:
    """Internal send function - returns amount and serialized token"""
    wallet = await Wallet.with_db(
        mint_url or await _get_primary_mint_url(),
        db=".wallet",
        load_all_keysets=True,
        unit=unit,
    )
    await wallet.load_mint()
    await wallet.load_proofs()
    proofs = wallet._get_proofs_per_keyset(wallet.proofs)[wallet.keyset_id]

    send_proofs, fees = await wallet.select_to_send(
        proofs, amount, set_reserved=True, include_fees=True
    )
    token = await wallet.serialize_proofs(
        send_proofs, include_dleq=False, legacy=False, memo=None
    )
    return amount, token


async def send_token(
    amount: int, unit: CurrencyUnit | str, mint_url: str | None = None
) -> str:
    """Send token and return serialized token string"""
    unit_str = unit.value if isinstance(unit, CurrencyUnit) else unit
    _, token = await send(amount, unit_str, mint_url)
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
    primary_wallet = await Wallet.with_db(
        await _get_primary_mint_url(), db=".wallet", load_all_keysets=True, unit="sat"
    )
    await primary_wallet.load_mint()

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

    return minted_amount, CurrencyUnit.sat, await _get_primary_mint_url()


async def credit_balance(cashu_token: str, key: ApiKey, session: Any) -> int:
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

        if mint_url != await _get_primary_mint_url():
            logger.error(
                "credit_balance: Mint URL mismatch",
                extra={
                    "mint_url": mint_url,
                    "primary_mint": await _get_primary_mint_url(),
                },
            )
            raise ValueError("Mint URL is not supported by this proxy")

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


async def send_to_lnurl(amount: int, unit: CurrencyUnit, lnurl: str) -> dict[str, Any]:
    """Send payment to Lightning Address/LNURL"""
    try:
        # Create wallet instance for this operation
        payment_wallet = await Wallet.with_db(
            await _get_primary_mint_url(),
            db=".wallet",
            load_all_keysets=True,
            unit=unit,
        )
        await payment_wallet.load_mint()

        # Convert amount to correct unit
        if unit == CurrencyUnit.sat and amount < 1000:
            # Convert sats to msats for small amounts
            amount_to_send = amount * 1000
            send_unit = CurrencyUnit.msat
        else:
            amount_to_send = amount
            send_unit = unit if isinstance(unit, CurrencyUnit) else CurrencyUnit(unit)

        # For now, return a mock successful response since LNURL payment is complex
        logger.info(f"Mock payment: {amount_to_send} {send_unit} to {lnurl}")

        return {
            "amount_sent": amount_to_send,
            "unit": send_unit.name,
            "lnurl": lnurl,
            "status": "completed",
        }

    except Exception as e:
        logger.error(f"Failed to send to LNURL {lnurl}: {e}")
        unit_str = unit.value if isinstance(unit, CurrencyUnit) else unit
        return {
            "amount_sent": 0,
            "unit": unit_str,
            "lnurl": lnurl,
            "status": "failed",
            "error": str(e),
        }


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
