from abc import ABC, abstractmethod
from typing import TypedDict

from sqlmodel.ext.asyncio.session import AsyncSession

from ..core.db import ApiKey
from ..core.logging import get_logger

logger = get_logger(__name__)


class PaymentResult(TypedDict):
    amount_msats: int
    currency: str
    mint_url: str | None
    payment_method: str


class PaymentMethod(ABC):
    """Abstract base class for payment methods used for temporary balance topups."""

    @property
    @abstractmethod
    def method_name(self) -> str:
        """Return the name identifier for this payment method."""
        pass

    @abstractmethod
    async def validate_payment_data(self, payment_data: str) -> bool:
        """Validate that the payment data is valid for this payment method.
        
        Args:
            payment_data: The raw payment data (e.g., cashu token, lightning invoice, etc.)
            
        Returns:
            True if the payment data is valid, False otherwise
        """
        pass

    @abstractmethod
    async def process_payment(
        self, payment_data: str, api_key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        """Process a payment and credit the balance to the API key.
        
        Args:
            payment_data: The raw payment data for this payment method
            api_key: The API key to credit the balance to
            session: Database session
            
        Returns:
            PaymentResult containing amount credited, currency, mint_url, and payment method
            
        Raises:
            ValueError: If payment data is invalid or payment fails
            Exception: For other payment processing errors
        """
        pass

    @abstractmethod
    async def get_supported_currencies(self) -> list[str]:
        """Return list of currencies supported by this payment method."""
        pass


class CashuPaymentMethod(PaymentMethod):
    """Payment method for Cashu eCash tokens."""

    @property
    def method_name(self) -> str:
        return "cashu"

    async def validate_payment_data(self, payment_data: str) -> bool:
        payment_data = payment_data.replace("\n", "").replace("\r", "").replace("\t", "")
        if len(payment_data) < 10 or "cashu" not in payment_data.lower():
            return False
        return True

    async def process_payment(
        self, payment_data: str, api_key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        from ..wallet import recieve_token

        payment_data = payment_data.replace("\n", "").replace("\r", "").replace("\t", "")
        if not await self.validate_payment_data(payment_data):
            raise ValueError("Invalid Cashu token format")

        try:
            amount, unit, mint_url = await recieve_token(payment_data)
            logger.info(
                "CashuPaymentMethod: Token redeemed successfully",
                extra={"amount": amount, "unit": unit, "mint_url": mint_url},
            )

            if unit == "sat":
                amount_msats = amount * 1000
            else:
                amount_msats = amount

            from sqlmodel import col, update

            stmt = (
                update(ApiKey)
                .where(col(ApiKey.hashed_key) == api_key.hashed_key)
                .values(balance=(ApiKey.balance) + amount_msats)
            )
            await session.exec(stmt)
            await session.commit()
            await session.refresh(api_key)

            logger.info(
                "CashuPaymentMethod: Balance updated successfully",
                extra={"new_balance": api_key.balance},
            )

            return PaymentResult(
                amount_msats=amount_msats,
                currency=unit,
                mint_url=mint_url,
                payment_method=self.method_name,
            )
        except Exception as e:
            logger.error(
                "CashuPaymentMethod: Error during token redemption",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            raise

    async def get_supported_currencies(self) -> list[str]:
        return ["sat", "msat"]


class BitcoinLightningPaymentMethod(PaymentMethod):
    """Payment method for Bitcoin Lightning Network invoices.
    
    TODO: Full implementation requires:
    1. Lightning node integration (e.g., LND, CLN, or LDK)
    2. Invoice validation and payment verification
    3. Webhook/event handling for payment confirmation
    4. Support for both BOLT-11 invoices and LNURL-pay
    5. Fee calculation and handling
    6. Timeout handling for unpaid invoices
    7. Integration with existing wallet infrastructure for balance management
    """

    @property
    def method_name(self) -> str:
        return "lightning"

    async def validate_payment_data(self, payment_data: str) -> bool:
        if not payment_data:
            return False
        invoice_lower = payment_data.lower().strip()
        if invoice_lower.startswith("ln") or invoice_lower.startswith("lightning:"):
            return True
        if "@" in payment_data and "." in payment_data:
            return True
        return False

    async def process_payment(
        self, payment_data: str, api_key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        """Pseudo-implementation of Lightning payment processing.
        
        TODO: Full implementation requires:
        1. Parse BOLT-11 invoice or LNURL-pay request
        2. Validate invoice amount and expiration
        3. Wait for payment confirmation via Lightning node
        4. Verify payment on-chain or via node
        5. Convert BTC amount to msats equivalent
        6. Credit balance to API key atomically
        7. Handle payment failures and timeouts
        """
        logger.warning(
            "BitcoinLightningPaymentMethod: Pseudo-implementation called",
            extra={"payment_data_preview": payment_data[:50]},
        )

        if not await self.validate_payment_data(payment_data):
            raise ValueError("Invalid Lightning invoice format")

        from .lnurl import parse_lightning_invoice_amount

        try:
            if payment_data.startswith("ln"):
                amount_sats = parse_lightning_invoice_amount(payment_data, currency="sat")
                amount_msats = amount_sats * 1000
            else:
                raise ValueError("Only BOLT-11 invoices are currently supported in pseudo-implementation")

            logger.info(
                "BitcoinLightningPaymentMethod: Invoice parsed (pseudo)",
                extra={"amount_sats": amount_sats, "amount_msats": amount_msats},
            )

            raise NotImplementedError(
                "Lightning payment processing requires Lightning node integration. "
                "See BitcoinLightningPaymentMethod.process_payment docstring for implementation details."
            )

        except Exception as e:
            logger.error(
                "BitcoinLightningPaymentMethod: Error processing payment",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            raise

    async def get_supported_currencies(self) -> list[str]:
        return ["sat", "msat"]


class USDTetherPaymentMethod(PaymentMethod):
    """Payment method for USDT (Tether) payments.
    
    TODO: Full implementation requires:
    1. Integration with USDT payment processor (e.g., Tether API, exchange API, or on-chain monitoring)
    2. Support for multiple networks (TRC-20 on Tron, ERC-20 on Ethereum, etc.)
    3. Address generation and monitoring for incoming payments
    4. Payment verification via blockchain explorer API or node
    5. Exchange rate conversion from USDT to msats
    6. Webhook handling for payment confirmations
    7. Multi-signature wallet support for security
    8. Gas fee handling for on-chain transactions
    9. Integration with existing balance management system
    """

    @property
    def method_name(self) -> str:
        return "usdt"

    async def validate_payment_data(self, payment_data: str) -> bool:
        if not payment_data:
            return False
        payment_data = payment_data.strip()
        if payment_data.startswith("0x") and len(payment_data) == 66:
            return True
        if payment_data.startswith("T") and len(payment_data) == 34:
            return True
        return False

    async def process_payment(
        self, payment_data: str, api_key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        """Pseudo-implementation of USDT payment processing.
        
        TODO: Full implementation requires:
        1. Parse payment data (transaction hash, address, or payment ID)
        2. Query blockchain/API to verify payment
        3. Get USDT amount from transaction
        4. Convert USDT to msats using current exchange rate
        5. Verify sufficient confirmations (network-dependent)
        6. Credit balance to API key atomically
        7. Handle network-specific fees and gas costs
        """
        logger.warning(
            "USDTetherPaymentMethod: Pseudo-implementation called",
            extra={"payment_data_preview": payment_data[:50]},
        )

        if not await self.validate_payment_data(payment_data):
            raise ValueError("Invalid USDT payment data format")

        logger.info(
            "USDTetherPaymentMethod: Payment data validated (pseudo)",
            extra={"payment_data": payment_data},
        )

        raise NotImplementedError(
            "USDT payment processing requires blockchain integration. "
            "See USDTetherPaymentMethod.process_payment docstring for implementation details."
        )

    async def get_supported_currencies(self) -> list[str]:
        return ["usdt"]


def get_payment_method(method_name: str) -> PaymentMethod:
    """Factory function to get a payment method by name.
    
    Args:
        method_name: Name of the payment method ("cashu", "lightning", "usdt")
        
    Returns:
        PaymentMethod instance
        
    Raises:
        ValueError: If payment method is not supported
    """
    method_name_lower = method_name.lower()
    
    if method_name_lower == "cashu":
        return CashuPaymentMethod()
    elif method_name_lower == "lightning":
        return BitcoinLightningPaymentMethod()
    elif method_name_lower in ("usdt", "usdtether", "tether"):
        return USDTetherPaymentMethod()
    else:
        raise ValueError(f"Unsupported payment method: {method_name}")


def detect_payment_method(payment_data: str) -> str:
    """Auto-detect payment method from payment data.
    
    Args:
        payment_data: Raw payment data string
        
    Returns:
        Payment method name ("cashu", "lightning", "usdt", or "unknown")
    """
    payment_data_lower = payment_data.lower().strip()
    
    if "cashu" in payment_data_lower:
        return "cashu"
    elif payment_data_lower.startswith("ln") or payment_data_lower.startswith("lightning:"):
        return "lightning"
    elif payment_data_lower.startswith("0x") or payment_data_lower.startswith("t"):
        if len(payment_data) == 66 or len(payment_data) == 34:
            return "usdt"
    
    return "unknown"
