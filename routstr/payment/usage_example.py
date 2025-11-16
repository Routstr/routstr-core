"""
Example usage of the modular payment system.

This file demonstrates how to use the payment providers in your application.
Run this example after initializing the providers.
"""

import asyncio

from routstr.payment import (
    PaymentMethodType,
    auto_detect_and_redeem,
    get_provider_by_type,
    get_provider_for_token,
    initialize_payment_providers,
    list_available_payment_methods,
)


async def example_basic_usage() -> None:
    """Basic example: auto-detect and redeem a token."""
    print("=== Basic Usage Example ===\n")

    # Initialize payment providers (do this once at startup)
    initialize_payment_providers()

    # Example Cashu token (would be user-provided in production)
    cashu_token = "cashuAeyJ0b2tlbiI6W3sibWludCI6Imh0dHA6Ly9sb2NhbGhvc3Q6MzMzOCIsInByb29mcyI6W3siaWQiOiIwMDk..."

    try:
        # Auto-detect payment method and redeem
        amount, currency, source = await auto_detect_and_redeem(cashu_token)

        print(f"✅ Successfully redeemed payment!")
        print(f"   Amount: {amount} {currency}")
        print(f"   Source: {source}\n")

    except ValueError as e:
        print(f"❌ Redemption failed: {e}\n")


async def example_specific_provider() -> None:
    """Example: Use a specific payment provider."""
    print("=== Specific Provider Example ===\n")

    # Get the Cashu provider
    cashu_provider = get_provider_by_type(PaymentMethodType.CASHU)

    if not cashu_provider:
        print("❌ Cashu provider not available\n")
        return

    print(f"✅ Got provider: {cashu_provider.method_type.value}\n")

    # Example token
    token = "cashuAeyJ0b2tlbiI6..."

    # Validate token format
    is_valid = await cashu_provider.validate_token(token)
    print(f"Token validation: {is_valid}\n")

    if is_valid:
        try:
            # Parse token to get details
            payment = await cashu_provider.parse_token(token)

            print(f"Token details:")
            print(f"  Amount: {payment.amount_msats} msats")
            print(f"  Currency: {payment.currency}")
            print(f"  Method: {payment.method_type.value}")
            if payment.mint_url:
                print(f"  Mint: {payment.mint_url}")
            print()

        except ValueError as e:
            print(f"❌ Failed to parse token: {e}\n")


async def example_balance_check() -> None:
    """Example: Check if token has sufficient balance."""
    print("=== Balance Check Example ===\n")

    token = "cashuAeyJ0b2tlbiI6..."
    required_msats = 10000  # Require 10 sats (10,000 msats)

    # Auto-detect provider
    provider = await get_provider_for_token(token)

    if not provider:
        print("❌ No provider found for this token\n")
        return

    print(f"✅ Detected provider: {provider.method_type.value}")

    # Check if token has sufficient balance
    is_sufficient = await provider.check_balance_sufficiency(token, required_msats)

    if is_sufficient:
        print(f"✅ Token has sufficient balance (>= {required_msats} msats)\n")
    else:
        print(f"❌ Token has insufficient balance (< {required_msats} msats)\n")


async def example_refund() -> None:
    """Example: Create a refund."""
    print("=== Refund Example ===\n")

    cashu_provider = get_provider_by_type(PaymentMethodType.CASHU)

    if not cashu_provider:
        print("❌ Cashu provider not available\n")
        return

    try:
        # Create refund token
        refund = await cashu_provider.create_refund(
            amount=1000,  # 1000 sats
            currency="sat",
            destination=None,  # None = create token, otherwise send to LNURL
        )

        print(f"✅ Refund created!")
        print(f"   Amount: {refund.amount_msats} msats")
        print(f"   Currency: {refund.currency}")

        if refund.token:
            print(f"   Token: {refund.token[:50]}...")
        if refund.destination:
            print(f"   Destination: {refund.destination}")
        print()

    except Exception as e:
        print(f"❌ Refund failed: {e}\n")


async def example_list_methods() -> None:
    """Example: List all available payment methods."""
    print("=== Available Payment Methods ===\n")

    methods = list_available_payment_methods()

    print(f"Total payment methods: {len(methods)}\n")

    for method in methods:
        provider = get_provider_by_type(method)
        status = "✅ Functional" if method == PaymentMethodType.CASHU else "🚧 Pseudo"
        print(f"  {status} - {method.value}")

        if provider:
            print(f"    Provider: {provider.__class__.__name__}")

    print()


async def example_lightning_pseudo() -> None:
    """Example: Try to use Lightning provider (pseudo implementation)."""
    print("=== Lightning Provider Example (Pseudo) ===\n")

    lightning_provider = get_provider_by_type(PaymentMethodType.LIGHTNING)

    if not lightning_provider:
        print("❌ Lightning provider not available\n")
        return

    print(f"✅ Lightning provider available: {lightning_provider.__class__.__name__}")

    # Example Lightning invoice
    invoice = "lnbc10u1p3..."

    # Validate format
    is_valid = await lightning_provider.validate_token(invoice)
    print(f"Invoice format valid: {is_valid}\n")

    if is_valid:
        try:
            # This will raise NotImplementedError
            await lightning_provider.parse_token(invoice)
        except NotImplementedError as e:
            print(f"⚠️  Expected NotImplementedError: {e}\n")


async def main() -> None:
    """Run all examples."""
    print("\n" + "=" * 60)
    print("Payment System Usage Examples")
    print("=" * 60 + "\n")

    await example_list_methods()
    await example_basic_usage()
    await example_specific_provider()
    await example_balance_check()
    await example_refund()
    await example_lightning_pseudo()

    print("=" * 60)
    print("Examples complete!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    # Run the examples
    # Note: In production, initialize_payment_providers() should be called
    # during application startup, not in each function
    asyncio.run(main())
