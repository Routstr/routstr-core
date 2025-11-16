from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from sqlmodel import col, update

from ..core import get_logger
from ..core.db import ApiKey, AsyncSession
from ..wallet import recieve_token

logger = get_logger(__name__)


@dataclass(slots=True)
class TemporaryBalancePaymentPayload:
    method: str
    data: dict[str, Any]

    def get_str(self, key: str) -> str:
        value = self.data.get(key)
        if not isinstance(value, str):
            raise ValueError(f"{key} is required for {self.method} payments")
        return value


class TemporaryBalancePaymentMethod(abc.ABC):
    method: str
    display_name: str

    def supports(self, payload: TemporaryBalancePaymentPayload) -> bool:
        return payload.method == self.method

    @abc.abstractmethod
    async def credit(
        self, payload: TemporaryBalancePaymentPayload, key: ApiKey, session: AsyncSession
    ) -> int:
        ...


class TemporaryBalancePaymentRegistry:
    def __init__(self) -> None:
        self._methods: dict[str, TemporaryBalancePaymentMethod] = {}

    def register(self, method: TemporaryBalancePaymentMethod) -> None:
        self._methods[method.method] = method

    def get(self, method: str) -> TemporaryBalancePaymentMethod:
        if method not in self._methods:
            raise ValueError(f"Unsupported payment method '{method}'")
        return self._methods[method]

    def list_methods(self) -> list[str]:
        return list(self._methods.keys())


temporary_balance_payments = TemporaryBalancePaymentRegistry()


async def credit_temporary_balance(
    payload: TemporaryBalancePaymentPayload, key: ApiKey, session: AsyncSession
) -> int:
    method = temporary_balance_payments.get(payload.method)
    return await method.credit(payload, key, session)


def build_cashu_payment_payload(token: str) -> TemporaryBalancePaymentPayload:
    return TemporaryBalancePaymentPayload(method="cashu", data={"token": token})


class CashuTokenPaymentMethod(TemporaryBalancePaymentMethod):
    method = "cashu"
    display_name = "Cashu eCash"

    async def credit(
        self, payload: TemporaryBalancePaymentPayload, key: ApiKey, session: AsyncSession
    ) -> int:
        token_raw = payload.get_str("token")
        token_clean = (
            token_raw.replace("\n", "").replace("\r", "").replace("\t", "").strip()
        )
        if len(token_clean) < 10 or "cashu" not in token_clean:
            raise ValueError("Invalid token format")

        logger.info(
            "credit_balance: Starting token redemption",
            extra={"token_preview": token_clean[:50]},
        )

        try:
            amount, unit, mint_url = await recieve_token(token_clean)
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

            stmt = (
                update(ApiKey)
                .where(col(ApiKey.hashed_key) == key.hashed_key)
                .values(balance=(ApiKey.balance) + amount)
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


class LightningInvoicePaymentMethod(TemporaryBalancePaymentMethod):
    method = "lightning"
    display_name = "Bitcoin Lightning"

    async def credit(
        self, payload: TemporaryBalancePaymentPayload, key: ApiKey, session: AsyncSession
    ) -> int:
        # Full implementation would need to: 1) generate invoices, 2) persist pending states,
        # 3) watch the lightning node or service for settlement events, 4) settle credit atomically.
        raise NotImplementedError(
            "Lightning-based temporary balances are not available yet."
        )


class UsdtCustodialPaymentMethod(TemporaryBalancePaymentMethod):
    method = "usdt"
    display_name = "USD Tether"

    async def credit(
        self, payload: TemporaryBalancePaymentPayload, key: ApiKey, session: AsyncSession
    ) -> int:
        # Full implementation would need to: 1) integrate with a custodian or on-chain monitor,
        # 2) handle stablecoin confirmations, 3) price conversions to msats, 4) settle and refund.
        raise NotImplementedError(
            "USDT-based temporary balances are not available yet."
        )


temporary_balance_payments.register(CashuTokenPaymentMethod())
temporary_balance_payments.register(LightningInvoicePaymentMethod())
temporary_balance_payments.register(UsdtCustodialPaymentMethod())
