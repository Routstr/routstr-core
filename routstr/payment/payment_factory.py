"""
Payment method factory and initialization.

This module provides convenience functions for initializing and accessing
payment method providers.
"""

from ..core.logging import get_logger
from .bitcoin_onchain_payment import BitcoinOnChainPaymentProvider
from .cashu_payment import CashuPaymentProvider
from .lightning_payment import LightningPaymentProvider
from .payment_method import (
    PaymentMethodProvider,
    PaymentMethodRegistry,
    PaymentMethodType,
    get_payment_registry,
)
from .tether_payment import TetherPaymentProvider

logger = get_logger(__name__)

_initialized = False


def initialize_payment_providers() -> PaymentMethodRegistry:
    """
    Initialize and register all payment method providers.

    This function should be called during application startup to register
    all available payment methods. Currently only Cashu is fully implemented;
    other methods are pseudo-implementations for demonstration.

    Returns:
        The global payment method registry with all providers registered
    """
    global _initialized

    if _initialized:
        logger.debug("Payment providers already initialized")
        return get_payment_registry()

    registry = get_payment_registry()

    # Register Cashu provider (fully implemented)
    cashu_provider = CashuPaymentProvider()
    registry.register(cashu_provider)
    logger.info("Registered Cashu payment provider")

    # Register Lightning provider (pseudo-implementation)
    lightning_provider = LightningPaymentProvider()
    registry.register(lightning_provider)
    logger.info(
        "Registered Lightning payment provider (PSEUDO - not functional)",
    )

    # Register Tether provider (pseudo-implementation)
    tether_provider = TetherPaymentProvider()
    registry.register(tether_provider)
    logger.info(
        "Registered USDT Tether payment provider (PSEUDO - not functional)",
    )

    # Register Bitcoin on-chain provider (pseudo-implementation)
    btc_onchain_provider = BitcoinOnChainPaymentProvider()
    registry.register(btc_onchain_provider)
    logger.info(
        "Registered Bitcoin on-chain payment provider (PSEUDO - not functional)",
    )

    _initialized = True
    logger.info(
        "Payment provider initialization complete",
        extra={"total_providers": len(registry.list_supported_methods())},
    )

    return registry


def get_provider_for_token(token: str) -> PaymentMethodProvider | None:
    """
    Get the appropriate payment provider for a token.

    This is a convenience function that automatically detects which provider
    can handle the given token.

    Args:
        token: Payment token string

    Returns:
        The appropriate provider, or None if no provider can handle the token

    Example:
        ```python
        provider = await get_provider_for_token("cashuAey...")
        if provider:
            amount, currency, source = await provider.redeem_token(token)
        ```
    """
    registry = get_payment_registry()
    return registry.detect_provider(token)


def get_provider_by_type(
    method_type: PaymentMethodType,
) -> PaymentMethodProvider | None:
    """
    Get a payment provider by its type.

    Args:
        method_type: The payment method type

    Returns:
        The provider for that type, or None if not registered

    Example:
        ```python
        cashu_provider = get_provider_by_type(PaymentMethodType.CASHU)
        lightning_provider = get_provider_by_type(PaymentMethodType.LIGHTNING)
        ```
    """
    registry = get_payment_registry()
    return registry.get_provider(method_type)


def list_available_payment_methods() -> list[PaymentMethodType]:
    """
    List all available payment method types.

    Returns:
        List of registered payment method types

    Example:
        ```python
        methods = list_available_payment_methods()
        # Returns: [PaymentMethodType.CASHU, PaymentMethodType.LIGHTNING, ...]
        ```
    """
    registry = get_payment_registry()
    return registry.list_supported_methods()


async def auto_detect_and_redeem(token: str) -> tuple[int, str, str]:
    """
    Automatically detect payment method and redeem token.

    This is a high-level convenience function that:
    1. Detects which provider can handle the token
    2. Redeems the token using that provider

    Args:
        token: Payment token string

    Returns:
        Tuple of (amount, currency, source_identifier)

    Raises:
        ValueError: If no provider can handle the token or redemption fails

    Example:
        ```python
        try:
            amount, currency, source = await auto_detect_and_redeem(token)
            print(f"Redeemed {amount} {currency} from {source}")
        except ValueError as e:
            print(f"Failed to redeem: {e}")
        ```
    """
    provider = await get_provider_for_token(token)

    if not provider:
        raise ValueError(
            "No payment provider found for this token. "
            "Token format not recognized or provider not registered."
        )

    logger.info(
        "Auto-detected payment method",
        extra={
            "method_type": provider.method_type.value,
            "token_preview": token[:20] + "...",
        },
    )

    return await provider.redeem_token(token)
