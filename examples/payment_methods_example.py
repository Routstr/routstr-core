"""
Example usage of the modular payment method system.

This demonstrates how to use different payment methods for temporary balance management.
"""

import asyncio
from routstr.payment.methods import PaymentMethodFactory


async def example_cashu_payment():
    """Example: Using Cashu payment method (fully implemented)"""
    print("=" * 60)
    print("Example 1: Cashu Payment Method")
    print("=" * 60)
    
    # Get Cashu payment method
    cashu = PaymentMethodFactory.get_method("cashu")
    print(f"Payment method: {cashu.method_name}")
    
    # Example Cashu token (this is a dummy token for demonstration)
    cashu_token = "cashuAeyJ0b2tlbiI6W3sibWludCI6Imh0dHBzOi8vbWludC5leGFtcGxlLmNvbSIsInByb29mcyI6W119XX0="
    
    # Validate token format
    is_valid = await cashu.validate_token(cashu_token)
    print(f"Token valid: {is_valid}")
    
    # Receive payment (would process the token if it was real)
    try:
        payment_info = await cashu.receive(cashu_token)
        print(f"Received: {payment_info['amount']} {payment_info['unit']}")
        print(f"From mint: {payment_info['mint_url']}")
    except Exception as e:
        print(f"Expected error (demo token): {type(e).__name__}: {e}")
    
    print()


async def example_auto_detection():
    """Example: Auto-detecting payment method from token"""
    print("=" * 60)
    print("Example 2: Auto-Detection of Payment Method")
    print("=" * 60)
    
    test_tokens = [
        ("cashuAeyJ0b2...", "Cashu token"),
        ("lnbc10u1p3...", "Lightning invoice"),
        ("user@domain.com", "Lightning address"),
        ("0x1234567890abcdef" * 4, "USDT on Ethereum"),
        ("a" * 64, "Bitcoin transaction"),
    ]
    
    for token, description in test_tokens:
        try:
            method = PaymentMethodFactory.detect_method(token)
            print(f"✓ {description:25} → {method.method_name}")
        except ValueError as e:
            print(f"✗ {description:25} → Could not detect")
    
    print()


async def example_lightning_payment():
    """Example: Lightning payment method (pseudo-implemented)"""
    print("=" * 60)
    print("Example 3: Lightning Payment Method (Not Implemented Yet)")
    print("=" * 60)
    
    lightning = PaymentMethodFactory.get_method("lightning")
    print(f"Payment method: {lightning.method_name}")
    
    # Lightning invoice
    invoice = "lnbc10u1p3pj257pp5ynxsq0w..."
    
    # Validate format
    is_valid = await lightning.validate_token(invoice)
    print(f"Invoice format valid: {is_valid}")
    
    # Try to receive (will fail with NotImplementedError)
    try:
        payment_info = await lightning.receive(invoice)
        print(f"Received: {payment_info['amount']} msats")
    except NotImplementedError as e:
        print(f"Expected: NotImplementedError - {e}")
    
    print()


async def example_usdt_payment():
    """Example: USDT payment method (pseudo-implemented)"""
    print("=" * 60)
    print("Example 4: USDT Payment Method (Not Implemented Yet)")
    print("=" * 60)
    
    usdt = PaymentMethodFactory.get_method("usdt")
    print(f"Payment method: {usdt.method_name}")
    
    # Ethereum transaction hash
    tx_hash = "0x1234567890abcdef" * 4
    
    # Validate format
    is_valid = await usdt.validate_token(tx_hash)
    print(f"Transaction hash valid: {is_valid}")
    
    # Try to receive (will fail with NotImplementedError)
    try:
        payment_info = await usdt.receive(tx_hash)
        print(f"Received: {payment_info['amount']} msats")
    except NotImplementedError as e:
        print(f"Expected: NotImplementedError - {e}")
    
    print()


async def example_list_methods():
    """Example: List available and implemented payment methods"""
    print("=" * 60)
    print("Example 5: Available Payment Methods")
    print("=" * 60)
    
    available = PaymentMethodFactory.get_available_methods()
    implemented = PaymentMethodFactory.get_implemented_methods()
    
    print(f"Available payment methods: {', '.join(available)}")
    print(f"Fully implemented: {', '.join(implemented)}")
    
    print("\nStatus of each method:")
    for method_name in available:
        status = "✓ Implemented" if method_name in implemented else "⚠ Pseudo-implemented"
        print(f"  {method_name:15} {status}")
    
    print()


async def example_api_integration():
    """Example: How the API uses payment methods"""
    print("=" * 60)
    print("Example 6: API Integration")
    print("=" * 60)
    
    print("API endpoint: POST /v1/balance/topup")
    print()
    
    # Example 1: Auto-detection (backward compatible)
    print("Request 1: Auto-detection (backward compatible)")
    print('  {"cashu_token": "cashuAeyJ0..."}')
    print("  → Auto-detects Cashu method from token format")
    print()
    
    # Example 2: Explicit method specification
    print("Request 2: Explicit method specification")
    print('  {"cashu_token": "lnbc10u1...", "payment_method": "lightning"}')
    print("  → Uses Lightning method explicitly")
    print()
    
    # Example 3: Future USDT support
    print("Request 3: Future USDT support")
    print('  {"cashu_token": "0x1234...", "payment_method": "usdt"}')
    print("  → Would use USDT method when implemented")
    print()


async def main():
    """Run all examples"""
    print("\n")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 10 + "ROUTSTR PAYMENT METHODS EXAMPLES" + " " * 16 + "║")
    print("╚" + "═" * 58 + "╝")
    print()
    
    await example_cashu_payment()
    await example_auto_detection()
    await example_lightning_payment()
    await example_usdt_payment()
    await example_list_methods()
    await example_api_integration()
    
    print("=" * 60)
    print("For more information, see:")
    print("  - docs/advanced/payment-methods.md")
    print("  - routstr/payment/README.md")
    print("=" * 60)
    print()


if __name__ == "__main__":
    asyncio.run(main())
