"""
Abstract payment method interface for temporary balance orchestration.

This module defines the abstract base classes for implementing different payment methods
(Cashu, Bitcoin Lightning, USDT Tether, etc.) in a modular way.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class PaymentMethodType(str, Enum):
    """Supported payment method types."""

    CASHU = "cashu"
    LIGHTNING = "lightning"
    TETHER = "tether"
    BITCOIN_ONCHAIN = "bitcoin_onchain"


@dataclass
class PaymentToken:
    """Represents a payment token with its metadata."""

    raw_token: str
    amount_msats: int
    currency: str
    mint_url: str | None = None
    method_type: PaymentMethodType | None = None


@dataclass
class RefundDetails:
    """Details for refunding a payment."""

    amount_msats: int
    currency: str
    destination: str | None = None
    token: str | None = None


class PaymentMethodProvider(ABC):
    """
    Abstract base class for payment method providers.

    Each payment method (Cashu, Lightning, Tether, etc.) must implement this interface
    to handle token validation, redemption, and refunding.
    """

    @property
    @abstractmethod
    def method_type(self) -> PaymentMethodType:
        """Return the payment method type identifier."""
        pass

    @abstractmethod
    async def validate_token(self, token: str) -> bool:
        """
        Validate if a token string belongs to this payment method.

        Args:
            token: The payment token string to validate

        Returns:
            True if this provider can handle the token, False otherwise
        """
        pass

    @abstractmethod
    async def parse_token(self, token: str) -> PaymentToken:
        """
        Parse a payment token string into structured data.

        Args:
            token: The payment token string to parse

        Returns:
            PaymentToken object with amount, currency, and metadata

        Raises:
            ValueError: If token format is invalid
        """
        pass

    @abstractmethod
    async def redeem_token(self, token: str) -> tuple[int, str, str]:
        """
        Redeem a payment token and credit the balance.

        Args:
            token: The payment token string to redeem

        Returns:
            Tuple of (amount_in_base_unit, currency, source_identifier)
            For example: (1000, "sat", "mint_url") or (50000, "msat", "node_pubkey")

        Raises:
            ValueError: If redemption fails (invalid token, already spent, etc.)
        """
        pass

    @abstractmethod
    async def create_refund(
        self, amount: int, currency: str, destination: str | None = None
    ) -> RefundDetails:
        """
        Create a refund payment.

        Args:
            amount: Amount to refund in the base unit (sats or msats)
            currency: Currency unit (e.g., "sat", "msat", "usdt")
            destination: Optional destination address/LNURL for the refund

        Returns:
            RefundDetails with token or destination information

        Raises:
            ValueError: If refund creation fails
        """
        pass

    @abstractmethod
    async def check_balance_sufficiency(
        self, token: str, required_amount_msats: int
    ) -> bool:
        """
        Check if a token has sufficient balance for a request.

        Args:
            token: The payment token string
            required_amount_msats: Required amount in millisatoshis

        Returns:
            True if token has sufficient balance, False otherwise
        """
        pass


class PaymentMethodRegistry:
    """
    Registry for managing multiple payment method providers.

    This allows the system to support multiple payment methods simultaneously
    and route tokens to the appropriate provider.
    """

    def __init__(self) -> None:
        self._providers: dict[PaymentMethodType, PaymentMethodProvider] = {}

    def register(self, provider: PaymentMethodProvider) -> None:
        """Register a payment method provider."""
        self._providers[provider.method_type] = provider

    def get_provider(
        self, method_type: PaymentMethodType
    ) -> PaymentMethodProvider | None:
        """Get a specific payment method provider by type."""
        return self._providers.get(method_type)

    async def detect_provider(self, token: str) -> PaymentMethodProvider | None:
        """
        Automatically detect which provider can handle a token.

        Args:
            token: The payment token string

        Returns:
            The appropriate provider, or None if no provider matches
        """
        for provider in self._providers.values():
            if await provider.validate_token(token):
                return provider
        return None

    def list_supported_methods(self) -> list[PaymentMethodType]:
        """List all registered payment method types."""
        return list(self._providers.keys())


# Global registry instance
_registry = PaymentMethodRegistry()


def get_payment_registry() -> PaymentMethodRegistry:
    """Get the global payment method registry."""
    return _registry
