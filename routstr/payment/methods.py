from abc import ABC, abstractmethod
from typing import TypedDict

from sqlmodel.ext.asyncio.session import AsyncSession

from ..core.db import ApiKey


class PaymentMethodMetadata(TypedDict, total=False):
    """Metadata specific to each payment method type."""
    mint_url: str | None
    currency: str | None
    invoice: str | None
    transaction_hash: str | None
    network: str | None
    contract_address: str | None


class PaymentResult(TypedDict):
    """Result of processing a payment."""
    amount_msats: int
    metadata: PaymentMethodMetadata


class RefundResult(TypedDict):
    """Result of processing a refund."""
    success: bool
    refund_identifier: str | None
    metadata: PaymentMethodMetadata


class PaymentMethod(ABC):
    """Abstract base class for payment methods used for temporary balances."""

    @property
    @abstractmethod
    def method_type(self) -> str:
        """Return the payment method type identifier (e.g., 'cashu', 'lightning', 'usdt')."""
        pass

    @abstractmethod
    async def validate_payment_data(
        self, payment_data: str, session: AsyncSession
    ) -> PaymentMethodMetadata:
        """Validate payment data and extract metadata.
        
        Args:
            payment_data: Payment data string (token, invoice, etc.)
            session: Database session
            
        Returns:
            PaymentMethodMetadata with extracted information
            
        Raises:
            ValueError: If payment data is invalid
        """
        pass

    @abstractmethod
    async def credit_balance(
        self, payment_data: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        """Credit balance from payment data.
        
        Args:
            payment_data: Payment data string (token, invoice, etc.)
            key: API key to credit
            session: Database session
            
        Returns:
            PaymentResult with credited amount and metadata
        """
        pass

    @abstractmethod
    async def refund_balance(
        self, key: ApiKey, amount_msats: int, session: AsyncSession
    ) -> RefundResult:
        """Refund remaining balance.
        
        Args:
            key: API key with balance to refund
            amount_msats: Amount to refund in millisatoshis
            session: Database session
            
        Returns:
            RefundResult with refund details
        """
        pass

    @abstractmethod
    def can_handle(self, payment_data: str) -> bool:
        """Check if this payment method can handle the given payment data.
        
        Args:
            payment_data: Payment data string to check
            
        Returns:
            True if this method can handle the payment data
        """
        pass
