"""
Cashu eCash payment method implementation.

This module implements the PaymentMethodProvider interface for Cashu tokens,
wrapping the existing wallet logic.
"""

from ..core.logging import get_logger
from ..core.settings import settings
from ..wallet import (
    deserialize_token_from_string,
    recieve_token,
    send_to_lnurl,
    send_token,
)
from .payment_method import (
    PaymentMethodProvider,
    PaymentMethodType,
    PaymentToken,
    RefundDetails,
)

logger = get_logger(__name__)


class CashuPaymentProvider(PaymentMethodProvider):
    """
    Cashu eCash payment provider.

    Handles Cashu token validation, redemption, and refunding using the
    existing Cashu wallet infrastructure.
    """

    @property
    def method_type(self) -> PaymentMethodType:
        return PaymentMethodType.CASHU

    async def validate_token(self, token: str) -> bool:
        """Check if token is a valid Cashu token format."""
        if not token or not isinstance(token, str):
            return False

        # Cashu tokens start with "cashu" prefix
        if not token.startswith("cashu"):
            return False

        # Basic length check
        if len(token) < 10:
            return False

        try:
            # Try to deserialize to verify structure
            deserialize_token_from_string(token)
            return True
        except Exception:
            return False

    async def parse_token(self, token: str) -> PaymentToken:
        """
        Parse a Cashu token into structured data.

        Args:
            token: Cashu token string (e.g., "cashuAey...")

        Returns:
            PaymentToken with amount, currency, and mint URL

        Raises:
            ValueError: If token format is invalid
        """
        try:
            token_obj = deserialize_token_from_string(token)

            # Convert to msats if needed
            amount_msats = (
                token_obj.amount * 1000
                if token_obj.unit == "sat"
                else token_obj.amount
            )

            return PaymentToken(
                raw_token=token,
                amount_msats=amount_msats,
                currency=token_obj.unit,
                mint_url=token_obj.mint,
                method_type=PaymentMethodType.CASHU,
            )
        except Exception as e:
            logger.error(
                "Failed to parse Cashu token",
                extra={"error": str(e), "token_preview": token[:20]},
            )
            raise ValueError(f"Invalid Cashu token format: {e}")

    async def redeem_token(self, token: str) -> tuple[int, str, str]:
        """
        Redeem a Cashu token and store it in the wallet.

        Args:
            token: Cashu token string

        Returns:
            Tuple of (amount, currency_unit, mint_url)

        Raises:
            ValueError: If token is invalid, already spent, or redemption fails
        """
        try:
            # Use existing wallet logic to receive and validate token
            amount, unit, mint_url = await recieve_token(token)

            logger.info(
                "Cashu token redeemed successfully",
                extra={"amount": amount, "unit": unit, "mint": mint_url},
            )

            return amount, unit, mint_url

        except ValueError as e:
            # Re-raise ValueError with original message
            logger.error("Cashu token redemption failed", extra={"error": str(e)})
            raise
        except Exception as e:
            logger.error(
                "Unexpected error during Cashu redemption",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            raise ValueError(f"Failed to redeem Cashu token: {e}")

    async def create_refund(
        self, amount: int, currency: str, destination: str | None = None
    ) -> RefundDetails:
        """
        Create a Cashu refund token or send to LNURL.

        Args:
            amount: Amount to refund (in the currency's base unit)
            currency: Currency unit ("sat" or "msat")
            destination: Optional LNURL destination address

        Returns:
            RefundDetails with either a token or destination info

        Raises:
            ValueError: If refund creation fails
        """
        try:
            if destination:
                # Send to LNURL destination
                await send_to_lnurl(
                    amount,
                    currency,
                    settings.primary_mint,
                    destination,
                )

                logger.info(
                    "Cashu refund sent to LNURL",
                    extra={
                        "amount": amount,
                        "currency": currency,
                        "destination": destination[:20] + "...",
                    },
                )

                return RefundDetails(
                    amount_msats=amount * 1000 if currency == "sat" else amount,
                    currency=currency,
                    destination=destination,
                )
            else:
                # Create refund token
                refund_token = await send_token(amount, currency, settings.primary_mint)

                logger.info(
                    "Cashu refund token created",
                    extra={
                        "amount": amount,
                        "currency": currency,
                        "token_preview": refund_token[:20] + "...",
                    },
                )

                return RefundDetails(
                    amount_msats=amount * 1000 if currency == "sat" else amount,
                    currency=currency,
                    token=refund_token,
                )

        except Exception as e:
            logger.error(
                "Failed to create Cashu refund",
                extra={
                    "error": str(e),
                    "amount": amount,
                    "currency": currency,
                },
            )
            raise ValueError(f"Failed to create Cashu refund: {e}")

    async def check_balance_sufficiency(
        self, token: str, required_amount_msats: int
    ) -> bool:
        """
        Check if a Cashu token has sufficient balance.

        Args:
            token: Cashu token string
            required_amount_msats: Required amount in millisatoshis

        Returns:
            True if token has sufficient balance
        """
        try:
            token_obj = deserialize_token_from_string(token)

            # Convert token amount to msats
            amount_msats = (
                token_obj.amount * 1000
                if token_obj.unit == "sat"
                else token_obj.amount
            )

            return amount_msats >= required_amount_msats

        except Exception as e:
            logger.warning(
                "Failed to check Cashu balance",
                extra={"error": str(e), "token_preview": token[:20]},
            )
            return False
