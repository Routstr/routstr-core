from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from ..core import get_logger
from ..core.db import ApiKey, AsyncSession
from ..core.settings import settings
from ..wallet import credit_balance, deserialize_token_from_string

logger = get_logger(__name__)


class TemporaryBalancePaymentMethod(ABC):
    """Interface for temporary balance payment methods."""

    method_name: str = "abstract"

    @abstractmethod
    def supports(self, bearer_key: str) -> bool:
        """Return True when this method can process the provided bearer key."""

    @abstractmethod
    async def ensure_api_key(
        self,
        bearer_key: str,
        session: AsyncSession,
        refund_address: str | None,
        key_expiry_time: int | None,
    ) -> ApiKey:
        """Return an ApiKey by creating or updating state for the bearer key."""


class CashuPaymentMethod(TemporaryBalancePaymentMethod):
    """Existing Cashu-backed temporary balance workflow."""

    method_name = "cashu"

    def supports(self, bearer_key: str) -> bool:
        return bearer_key.startswith("cashu")

    async def ensure_api_key(
        self,
        bearer_key: str,
        session: AsyncSession,
        refund_address: str | None,
        key_expiry_time: int | None,
    ) -> ApiKey:
        try:
            hashed_key = hashlib.sha256(bearer_key.encode()).hexdigest()
            token = deserialize_token_from_string(bearer_key)
        except Exception as exc:  # noqa: BLE001 - we need the original error
            logger.error(
                "Failed to decode Cashu token",
                extra={
                    "error": str(exc),
                    "token_preview": bearer_key[:20] + "..."
                    if len(bearer_key) > 20
                    else bearer_key,
                },
            )
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": f"Invalid or expired Cashu key: {str(exc)}",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            ) from exc

        if existing_key := await session.get(ApiKey, hashed_key):
            if key_expiry_time is not None:
                existing_key.key_expiry_time = key_expiry_time
            if refund_address is not None:
                existing_key.refund_address = refund_address
            return existing_key

        if token.mint in settings.cashu_mints:
            refund_currency = token.unit
            refund_mint_url = token.mint
        else:
            refund_currency = "sat"
            refund_mint_url = settings.primary_mint

        new_key = ApiKey(
            hashed_key=hashed_key,
            balance=0,
            refund_address=refund_address,
            key_expiry_time=key_expiry_time,
            refund_currency=refund_currency,
            refund_mint_url=refund_mint_url,
        )
        session.add(new_key)

        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            existing_key = await session.get(ApiKey, hashed_key)
            if not existing_key:
                raise Exception("Failed to fetch key after IntegrityError")  # noqa: TRY002
            if key_expiry_time is not None:
                existing_key.key_expiry_time = key_expiry_time
            if refund_address is not None:
                existing_key.refund_address = refund_address
            return existing_key

        msats = await credit_balance(bearer_key, new_key, session)
        if msats <= 0:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "Token redemption failed",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )

        await session.refresh(new_key)
        await session.commit()
        return new_key


class BitcoinLightningPaymentMethod(TemporaryBalancePaymentMethod):
    """Placeholder Lightning payment method."""

    method_name = "bitcoin-lightning"

    def supports(self, bearer_key: str) -> bool:
        return bearer_key.lower().startswith("lnbc") or bearer_key.lower().startswith(
            "lightning:"
        )

    async def ensure_api_key(
        self,
        bearer_key: str,
        session: AsyncSession,
        refund_address: str | None,
        key_expiry_time: int | None,
    ) -> ApiKey:
        logger.info(
            "Lightning payment placeholder invoked",
            extra={"token_preview": bearer_key[:20] + "..."},
        )
        # Full implementation would need to 1) decode the invoice, 2) verify settlement
        # with a Lightning node or service, and 3) credit the ApiKey once payment clears.
        raise HTTPException(
            status_code=501,
            detail={
                "error": {
                    "message": "Bitcoin Lightning payments are not yet supported",
                    "type": "payment_method_not_available",
                    "code": "lightning_not_implemented",
                }
            },
        )


class UsdtTetherPaymentMethod(TemporaryBalancePaymentMethod):
    """Placeholder USDT (on-chain / custodial) payment method."""

    method_name = "usdtether"

    def supports(self, bearer_key: str) -> bool:
        return bearer_key.lower().startswith("usdt:")

    async def ensure_api_key(
        self,
        bearer_key: str,
        session: AsyncSession,
        refund_address: str | None,
        key_expiry_time: int | None,
    ) -> ApiKey:
        logger.info(
            "USDT payment placeholder invoked",
            extra={"token_preview": bearer_key[:20] + "..."},
        )
        # A production-ready version would need to integrate with a stablecoin custody
        # provider, track confirmations, perform AML checks, and finally mint balances.
        raise HTTPException(
            status_code=501,
            detail={
                "error": {
                    "message": "USDT payments are not yet supported",
                    "type": "payment_method_not_available",
                    "code": "usdt_not_implemented",
                }
            },
        )


TEMPORARY_BALANCE_PAYMENT_METHODS: list[TemporaryBalancePaymentMethod] = [
    CashuPaymentMethod(),
    BitcoinLightningPaymentMethod(),
    UsdtTetherPaymentMethod(),
]

__all__ = [
    "TemporaryBalancePaymentMethod",
    "CashuPaymentMethod",
    "BitcoinLightningPaymentMethod",
    "UsdtTetherPaymentMethod",
    "TEMPORARY_BALANCE_PAYMENT_METHODS",
]
