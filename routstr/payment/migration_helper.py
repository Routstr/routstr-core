"""
Migration helper functions for transitioning to the modular payment system.

These functions provide backward-compatible wrappers that can be used as drop-in
replacements for the existing wallet functions, gradually migrating to the new
payment provider system.
"""

from ..core.db import ApiKey, AsyncSession
from ..core.logging import get_logger
from .payment_factory import get_provider_by_type, initialize_payment_providers
from .payment_method import PaymentMethodType

logger = get_logger(__name__)

# Ensure providers are initialized
_providers_initialized = False


def _ensure_initialized() -> None:
    """Ensure payment providers are initialized."""
    global _providers_initialized
    if not _providers_initialized:
        initialize_payment_providers()
        _providers_initialized = True


async def credit_balance_via_provider(
    token: str, key: ApiKey, session: AsyncSession
) -> int:
    """
    Credit balance using the payment provider system.

    This is a drop-in replacement for the original credit_balance function
    that uses the new modular payment system under the hood.

    Args:
        token: Payment token (currently only Cashu supported)
        key: API key to credit
        session: Database session

    Returns:
        Amount credited in msats

    Raises:
        ValueError: If token redemption fails

    Example:
        ```python
        # Old way:
        from routstr.wallet import credit_balance
        amount = await credit_balance(token, key, session)

        # New way (backward compatible):
        from routstr.payment.migration_helper import credit_balance_via_provider
        amount = await credit_balance_via_provider(token, key, session)
        ```
    """
    _ensure_initialized()

    logger.info(
        "credit_balance_via_provider: Starting token redemption",
        extra={"token_preview": token[:50]},
    )

    try:
        # Get Cashu provider (currently only one fully implemented)
        provider = get_provider_by_type(PaymentMethodType.CASHU)

        if not provider:
            raise ValueError("Cashu payment provider not available")

        # Redeem token using provider
        amount, unit, mint_url = await provider.redeem_token(token)

        logger.info(
            "credit_balance_via_provider: Token redeemed via provider",
            extra={"amount": amount, "unit": unit, "mint_url": mint_url},
        )

        # Convert to msats if needed
        if unit == "sat":
            amount = amount * 1000
            logger.info(
                "credit_balance_via_provider: Converted to msat",
                extra={"amount_msat": amount},
            )

        logger.info(
            "credit_balance_via_provider: Updating balance",
            extra={"old_balance": key.balance, "credit_amount": amount},
        )

        # Update balance atomically (same as original)
        from sqlmodel import col, update

        from ..core.db import ApiKey as ApiKeyModel

        stmt = (
            update(ApiKeyModel)
            .where(col(ApiKeyModel.hashed_key) == key.hashed_key)
            .values(balance=(ApiKeyModel.balance) + amount)
        )
        await session.exec(stmt)  # type: ignore[call-overload]
        await session.commit()
        await session.refresh(key)

        logger.info(
            "credit_balance_via_provider: Balance updated successfully",
            extra={"new_balance": key.balance},
        )

        return amount

    except Exception as e:
        logger.error(
            "credit_balance_via_provider: Error during token redemption",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise


async def create_refund_via_provider(
    amount: int, currency: str, destination: str | None = None, mint_url: str | None = None
) -> dict[str, str]:
    """
    Create refund using the payment provider system.

    This is a drop-in replacement for the original refund logic that uses
    the new modular payment system.

    Args:
        amount: Amount to refund (in base unit)
        currency: Currency unit ("sat" or "msat")
        destination: Optional LNURL destination
        mint_url: Optional mint URL (ignored, uses primary mint)

    Returns:
        Dict with 'token' or 'recipient' key

    Example:
        ```python
        # Old way:
        from routstr.wallet import send_token, send_to_lnurl
        if refund_address:
            await send_to_lnurl(amount, currency, mint, refund_address)
            result = {"recipient": refund_address}
        else:
            token = await send_token(amount, currency, mint)
            result = {"token": token}

        # New way (cleaner):
        from routstr.payment.migration_helper import create_refund_via_provider
        result = await create_refund_via_provider(amount, currency, destination)
        ```
    """
    _ensure_initialized()

    logger.info(
        "create_refund_via_provider: Creating refund",
        extra={"amount": amount, "currency": currency, "has_destination": bool(destination)},
    )

    try:
        # Get Cashu provider
        provider = get_provider_by_type(PaymentMethodType.CASHU)

        if not provider:
            raise ValueError("Cashu payment provider not available")

        # Create refund
        refund_details = await provider.create_refund(amount, currency, destination)

        # Build result dict
        result: dict[str, str] = {}

        if refund_details.token:
            result["token"] = refund_details.token
            logger.info(
                "create_refund_via_provider: Refund token created",
                extra={"token_preview": refund_details.token[:50] + "..."},
            )

        if refund_details.destination:
            result["recipient"] = refund_details.destination
            logger.info(
                "create_refund_via_provider: Refund sent to destination",
                extra={"destination": refund_details.destination[:20] + "..."},
            )

        return result

    except Exception as e:
        logger.error(
            "create_refund_via_provider: Error creating refund",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise


async def validate_token_format(token: str) -> bool:
    """
    Validate token format using payment providers.

    This checks if any registered payment provider can handle the token format.

    Args:
        token: Token string to validate

    Returns:
        True if token format is valid for any provider

    Example:
        ```python
        from routstr.payment.migration_helper import validate_token_format

        if await validate_token_format(token):
            # Token format is valid
            pass
        ```
    """
    _ensure_initialized()

    from .payment_factory import get_provider_for_token

    provider = await get_provider_for_token(token)
    return provider is not None


async def check_token_balance_via_provider(token: str, required_msats: int) -> bool:
    """
    Check if token has sufficient balance using payment providers.

    Args:
        token: Payment token
        required_msats: Required amount in millisatoshis

    Returns:
        True if token has sufficient balance

    Example:
        ```python
        from routstr.payment.migration_helper import check_token_balance_via_provider

        if await check_token_balance_via_provider(token, 10000):
            # Token has at least 10 sats
            pass
        ```
    """
    _ensure_initialized()

    from .payment_factory import get_provider_for_token

    provider = await get_provider_for_token(token)

    if not provider:
        return False

    return await provider.check_balance_sufficiency(token, required_msats)


# Backward compatibility: Re-export with original names
# These can be used as drop-in replacements in existing code
credit_balance_new = credit_balance_via_provider
send_refund_new = create_refund_via_provider
validate_token = validate_token_format
check_balance = check_token_balance_via_provider
