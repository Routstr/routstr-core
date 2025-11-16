from abc import ABC, abstractmethod
from typing import TypedDict

from ..core.db import ApiKey, AsyncSession


class PaymentResult(TypedDict):
    amount_msats: int
    payment_method: str
    payment_id: str | None


class PaymentMethod(ABC):
    """Abstract base class for payment methods used for temporary balance topups."""

    @property
    @abstractmethod
    def method_name(self) -> str:
        """Return the name identifier for this payment method."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Return a human-readable display name for this payment method."""
        pass

    @abstractmethod
    async def validate_payment_data(self, payment_data: str) -> bool:
        """Validate that the payment data is in the correct format for this method.

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
            PaymentResult containing amount credited, method name, and optional payment ID

        Raises:
            ValueError: If payment data is invalid or payment fails
            Exception: For other payment processing errors
        """
        pass

    @abstractmethod
    async def get_refund_info(
        self, api_key: ApiKey
    ) -> dict[str, str | None] | None:
        """Get refund information for this payment method if applicable.

        Args:
            api_key: The API key to get refund info for

        Returns:
            Dictionary with refund information or None if refunds not supported
        """
        pass


class CashuPaymentMethod(PaymentMethod):
    """Payment method for Cashu eCash tokens."""

    @property
    def method_name(self) -> str:
        return "cashu"

    @property
    def display_name(self) -> str:
        return "Cashu eCash"

    async def validate_payment_data(self, payment_data: str) -> bool:
        payment_data = payment_data.replace("\n", "").replace("\r", "").replace("\t", "")
        return len(payment_data) >= 10 and "cashu" in payment_data.lower()

    async def process_payment(
        self, payment_data: str, api_key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        from ..wallet import credit_balance

        payment_data = payment_data.replace("\n", "").replace("\r", "").replace("\t", "")
        if not await self.validate_payment_data(payment_data):
            raise ValueError("Invalid Cashu token format")

        amount_msats = await credit_balance(payment_data, api_key, session)
        return PaymentResult(
            amount_msats=amount_msats,
            payment_method=self.method_name,
            payment_id=None,
        )

    async def get_refund_info(
        self, api_key: ApiKey
    ) -> dict[str, str | None] | None:
        if api_key.refund_address:
            return {
                "refund_address": api_key.refund_address,
                "refund_mint_url": api_key.refund_mint_url,
                "refund_currency": api_key.refund_currency,
            }
        return None


class BitcoinLightningPaymentMethod(PaymentMethod):
    """Payment method for Bitcoin Lightning Network invoices."""

    @property
    def method_name(self) -> str:
        return "lightning"

    @property
    def display_name(self) -> str:
        return "Bitcoin Lightning"

    async def validate_payment_data(self, payment_data: str) -> bool:
        invoice = payment_data.strip().lower()
        return invoice.startswith("ln") and len(invoice) > 20

    async def process_payment(
        self, payment_data: str, api_key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        # TODO: Full implementation requires:
        # 1. Lightning node integration (LND, CLN, or similar)
        # 2. Invoice verification and payment status checking
        # 3. Webhook/callback system for payment confirmation
        # 4. Database schema to track pending payments
        # 5. Background task to poll payment status
        # 6. Integration with payment processor API (e.g., BTCPay Server, Strike, etc.)
        #
        # Example flow:
        # 1. Parse invoice to get amount_msats
        # 2. Verify invoice hasn't been paid
        # 3. Store pending payment in database with invoice_hash
        # 4. Wait for payment confirmation (via webhook or polling)
        # 5. Once confirmed, credit balance to api_key
        # 6. Return PaymentResult with payment_id = invoice_hash

        invoice = payment_data.strip()
        if not await self.validate_payment_data(invoice):
            raise ValueError("Invalid Lightning invoice format")

        # Pseudo-implementation: Parse invoice amount
        # In real implementation, use a Lightning library to decode invoice
        try:
            from ..payment.lnurl import parse_lightning_invoice_amount

            amount_msats = parse_lightning_invoice_amount(invoice, currency="msat")
        except Exception:
            raise ValueError("Failed to parse Lightning invoice amount")

        # Pseudo-implementation: In real system, this would:
        # 1. Check if invoice is already paid
        # 2. If not paid, store as pending payment
        # 3. Return pending status
        # 4. Credit balance once payment confirmed via webhook/polling

        raise NotImplementedError(
            "Lightning payment processing requires Lightning node integration. "
            "See method docstring for implementation requirements."
        )

    async def get_refund_info(
        self, api_key: ApiKey
    ) -> dict[str, str | None] | None:
        if api_key.refund_address and api_key.refund_address.startswith("ln"):
            return {
                "refund_address": api_key.refund_address,
                "refund_currency": api_key.refund_currency or "sat",
            }
        return None


class USDTPaymentMethod(PaymentMethod):
    """Payment method for USDT (Tether) on various blockchains."""

    @property
    def method_name(self) -> str:
        return "usdt"

    @property
    def display_name(self) -> str:
        return "USDT (Tether)"

    async def validate_payment_data(self, payment_data: str) -> bool:
        # TODO: Full implementation requires validation based on blockchain:
        # - Ethereum: 0x... address format (42 chars)
        # - Tron: T... address format (34 chars)
        # - Polygon: 0x... address format
        # - BSC: 0x... address format
        # - Solana: Base58 encoded address (32-44 chars)
        #
        # For now, basic validation
        payment_data = payment_data.strip()
        return (
            len(payment_data) >= 26
            and (payment_data.startswith("0x") or payment_data.startswith("T"))
        )

    async def process_payment(
        self, payment_data: str, api_key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        # TODO: Full implementation requires:
        # 1. Blockchain RPC integration (Ethereum, Tron, Polygon, BSC, Solana)
        # 2. USDT contract address for each chain
        # 3. Transaction monitoring system (webhook or polling)
        # 4. Exchange rate API to convert USDT to BTC/sats
        # 5. Database schema to track pending transactions
        # 6. Background task to monitor transaction confirmations
        # 7. Multi-signature or escrow system for security
        #
        # Example flow:
        # 1. User provides transaction hash or payment address
        # 2. Monitor blockchain for USDT transfer to our address
        # 3. Verify transaction amount and confirmations
        # 4. Convert USDT amount to BTC/sats using exchange rate
        # 5. Credit balance to api_key
        # 6. Return PaymentResult with payment_id = transaction_hash

        payment_data = payment_data.strip()
        if not await self.validate_payment_data(payment_data):
            raise ValueError("Invalid USDT payment data format")

        # Pseudo-implementation: In real system, this would:
        # 1. Parse transaction hash or payment address
        # 2. Query blockchain for transaction details
        # 3. Verify USDT transfer amount
        # 4. Get BTC/USDT exchange rate
        # 5. Convert to msats and credit balance

        raise NotImplementedError(
            "USDT payment processing requires blockchain integration. "
            "See method docstring for implementation requirements."
        )

    async def get_refund_info(
        self, api_key: ApiKey
    ) -> dict[str, str | None] | None:
        # USDT refunds would require blockchain address
        # This would need to be stored separately or in refund_address
        # with a prefix/format to identify it as USDT
        return None


def get_payment_method(method_name: str) -> PaymentMethod:
    """Factory function to get a payment method by name.

    Args:
        method_name: Name of the payment method (e.g., 'cashu', 'lightning', 'usdt')

    Returns:
        PaymentMethod instance

    Raises:
        ValueError: If payment method is not found
    """
    methods: dict[str, PaymentMethod] = {
        "cashu": CashuPaymentMethod(),
        "lightning": BitcoinLightningPaymentMethod(),
        "usdt": USDTPaymentMethod(),
    }

    if method_name.lower() not in methods:
        raise ValueError(
            f"Unknown payment method: {method_name}. "
            f"Available methods: {', '.join(methods.keys())}"
        )

    return methods[method_name.lower()]


def list_payment_methods() -> list[dict[str, str]]:
    """List all available payment methods.

    Returns:
        List of dictionaries with 'method' and 'display_name' keys
    """
    methods = [
        CashuPaymentMethod(),
        BitcoinLightningPaymentMethod(),
        USDTPaymentMethod(),
    ]
    return [
        {"method": m.method_name, "display_name": m.display_name} for m in methods
    ]
