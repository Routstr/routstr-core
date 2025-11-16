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
    name: str

    @abstractmethod
    def can_handle(self, bearer_key: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def provision(
        self,
        bearer_key: str,
        session: AsyncSession,
        *,
        refund_address: str | None,
        key_expiry_time: int | None,
    ) -> ApiKey:
        raise NotImplementedError


class CashuPaymentMethod(TemporaryBalancePaymentMethod):
    name = "cashu"

    def can_handle(self, bearer_key: str) -> bool:
        return bearer_key.startswith("cashu")

    async def provision(
        self,
        bearer_key: str,
        session: AsyncSession,
        *,
        refund_address: str | None,
        key_expiry_time: int | None,
    ) -> ApiKey:
        try:
            hashed_key = hashlib.sha256(bearer_key.encode()).hexdigest()
            token_obj = deserialize_token_from_string(bearer_key)
        except Exception as exc:
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
            self._apply_metadata(existing_key, refund_address, key_expiry_time)
            return existing_key

        if token_obj.mint in settings.cashu_mints:
            refund_currency = token_obj.unit
            refund_mint_url = token_obj.mint
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
                raise HTTPException(
                    status_code=500,
                    detail="Failed to finalize Cashu key creation",
                )
            self._apply_metadata(existing_key, refund_address, key_expiry_time)
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

    @staticmethod
    def _apply_metadata(
        key: ApiKey, refund_address: str | None, key_expiry_time: int | None
    ) -> None:
        if key_expiry_time is not None:
            key.key_expiry_time = key_expiry_time
        if refund_address is not None:
            key.refund_address = refund_address


class LightningInvoicePaymentMethod(TemporaryBalancePaymentMethod):
    name = "lightning"

    def can_handle(self, bearer_key: str) -> bool:
        normalized = bearer_key.lower()
        return normalized.startswith("lnbc") or normalized.startswith("lnurl")

    async def provision(
        self,
        bearer_key: str,
        session: AsyncSession,
        *,
        refund_address: str | None,
        key_expiry_time: int | None,
    ) -> ApiKey:
        # Full implementation would need invoice settlement proofs plus a swap service that returns sats-on-mint credits.
        raise HTTPException(
            status_code=501,
            detail="Lightning-backed temporary balances are not available yet.",
        )


class UsdtCustodialPaymentMethod(TemporaryBalancePaymentMethod):
    name = "usdt"

    def can_handle(self, bearer_key: str) -> bool:
        normalized = bearer_key.lower()
        return normalized.startswith("usdt") or normalized.startswith("tether:")

    async def provision(
        self,
        bearer_key: str,
        session: AsyncSession,
        *,
        refund_address: str | None,
        key_expiry_time: int | None,
    ) -> ApiKey:
        # Full implementation would require integrating with a custodial USDT issuer that can escrow funds and settle into msats.
        raise HTTPException(
            status_code=501,
            detail="USDT-backed temporary balances are not available yet.",
        )


_PAYMENT_METHODS: list[TemporaryBalancePaymentMethod] = [
    CashuPaymentMethod(),
    LightningInvoicePaymentMethod(),
    UsdtCustodialPaymentMethod(),
]


def resolve_temporary_balance_payment_method(
    bearer_key: str,
) -> TemporaryBalancePaymentMethod | None:
    for method in _PAYMENT_METHODS:
        if method.can_handle(bearer_key):
            logger.debug(
                "Selected temporary balance payment method",
                extra={"method": method.name},
            )
            return method
    return None
