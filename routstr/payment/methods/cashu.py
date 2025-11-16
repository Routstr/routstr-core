from cashu.wallet.helpers import deserialize_token_from_string
from sqlmodel import col, update

from ...core.db import ApiKey, AsyncSession
from ...core.logging import get_logger
from ...wallet import recieve_token, send_to_lnurl, send_token
from ..payment_methods import PaymentMethod, PaymentResult

logger = get_logger(__name__)


class CashuPaymentMethod(PaymentMethod):
    @property
    def name(self) -> str:
        return "cashu"

    @property
    def display_name(self) -> str:
        return "Cashu eCash"

    async def validate_payment_data(self, payment_data: str) -> bool:
        if not payment_data or len(payment_data) < 10:
            return False
        if "cashu" not in payment_data.lower():
            return False
        try:
            token_obj = deserialize_token_from_string(payment_data)
            return token_obj is not None
        except Exception:
            return False

    async def process_payment(
        self, payment_data: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        payment_data = payment_data.replace("\n", "").replace("\r", "").replace("\t", "")
        if not await self.validate_payment_data(payment_data):
            raise ValueError("Invalid Cashu token format")

        try:
            amount, unit, mint_url = await recieve_token(payment_data)
            logger.info(
                "Cashu payment processed",
                extra={"amount": amount, "unit": unit, "mint_url": mint_url},
            )

            if unit == "sat":
                amount_msats = amount * 1000
            else:
                amount_msats = amount

            stmt = (
                update(ApiKey)
                .where(col(ApiKey.hashed_key) == key.hashed_key)
                .values(balance=(ApiKey.balance) + amount_msats)
            )
            await session.exec(stmt)
            await session.commit()
            await session.refresh(key)

            metadata = {
                "mint_url": mint_url,
                "currency": unit,
            }

            return PaymentResult(
                amount_msats=amount_msats,
                currency=unit,
                payment_method=self.name,
                metadata=metadata,
            )
        except Exception as e:
            logger.error(
                "Cashu payment processing failed",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            raise

    async def refund_payment(
        self,
        amount_msats: int,
        currency: str,
        refund_address: str | None,
        metadata: dict[str, str | None],
        session: AsyncSession,
    ) -> dict[str, str]:
        mint_url = metadata.get("mint_url")
        if currency == "sat":
            remaining_balance = amount_msats // 1000
        else:
            remaining_balance = amount_msats

        if remaining_balance <= 0:
            raise ValueError("No balance to refund")

        try:
            if refund_address:
                from ...core.settings import settings as global_settings

                await send_to_lnurl(
                    remaining_balance,
                    currency or "sat",
                    mint_url or global_settings.primary_mint,
                    refund_address,
                )
                result = {"recipient": refund_address}
            else:
                token = await send_token(remaining_balance, currency, mint_url)
                result = {"token": token}

            if currency == "sat":
                result["sats"] = str(amount_msats // 1000)
            else:
                result["msats"] = str(amount_msats)

            return result
        except Exception as e:
            logger.error(
                "Cashu refund failed",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            raise

    def extract_metadata(self, payment_data: str) -> dict[str, str | None]:
        try:
            token_obj = deserialize_token_from_string(payment_data)
            return {
                "mint_url": token_obj.mint,
                "currency": token_obj.unit,
            }
        except Exception:
            return {
                "mint_url": None,
                "currency": None,
            }
