# Payment Module

This module contains the payment processing logic for Routstr, including multiple payment method implementations.

## Structure

```
payment/
├── __init__.py           # Module initialization
├── README.md             # This file
├── methods.py            # Payment method implementations (Cashu, Lightning, USDT, etc.)
├── cost_caculation.py    # Cost calculation utilities
├── helpers.py            # Payment helper functions
├── lnurl.py             # LNURL payment support
├── models.py            # Payment data models
└── price.py             # Pricing and rate conversion
```

## Payment Methods

The `methods.py` module provides a flexible architecture for supporting multiple payment types:

### Fully Implemented
- **CashuPaymentMethod** - eCash tokens via Cashu protocol

### Pseudo-Implemented (Ready for Development)
- **LightningPaymentMethod** - Direct Lightning Network payments
- **USDTPaymentMethod** - Tether stablecoin on Ethereum/Tron
- **OnChainBitcoinPaymentMethod** - Direct blockchain payments

## Quick Start

### Using Payment Methods

```python
from routstr.payment.methods import PaymentMethodFactory

# Auto-detect payment method from token
method = PaymentMethodFactory.detect_method(token)
payment_info = await method.receive(token)

# Or explicitly specify the method
cashu = PaymentMethodFactory.get_method("cashu")
payment_info = await cashu.receive(cashu_token)
```

### Implementing a New Payment Method

1. Create a class inheriting from `PaymentMethod`
2. Implement all abstract methods:
   - `method_name` - Unique identifier
   - `receive()` - Process incoming payments
   - `send()` - Send outgoing payments
   - `validate_token()` - Validate token format
   - `get_balance()` - Query balance
3. Register in `PaymentMethodFactory`
4. Add auto-detection logic
5. Write tests

See [docs/advanced/payment-methods.md](../../docs/advanced/payment-methods.md) for detailed implementation guide.

## Architecture Principles

### Abstraction
All payment methods implement the same interface, allowing seamless switching between different payment types.

### Modularity
Each payment method is self-contained with its own logic for:
- Token validation
- Payment reception
- Payment sending
- Balance tracking

### Extensibility
New payment methods can be added without modifying existing code - just implement the interface and register in the factory.

### Backward Compatibility
The system maintains full backward compatibility with existing Cashu-only implementations through auto-detection and fallback logic.

## Testing

Run payment method tests:

```bash
# Unit tests
pytest tests/unit/test_payment_methods.py

# Integration tests (requires running services)
pytest tests/integration/test_wallet_topup.py
```

## Security Considerations

When implementing payment methods:

1. **Never store private keys in code** - Use environment variables or HSM
2. **Always validate incoming payments** - Check confirmations, amounts, recipients
3. **Prevent double-crediting** - Store transaction IDs/hashes
4. **Use proper error handling** - Don't leak sensitive information
5. **Implement rate limiting** - Prevent abuse
6. **Log all transactions** - For audit trails

## Contributing

See [CONTRIBUTING.md](../../CONTRIBUTING.md) for guidelines on:
- Code style
- Testing requirements
- Pull request process
- Security disclosure

## Related Documentation

- [Payment Methods Guide](../../docs/advanced/payment-methods.md) - Detailed implementation guide
- [API Documentation](../../docs/api/endpoints.md) - API endpoint reference
- [User Guide](../../docs/user-guide/payment-flow.md) - End-user payment flow

## Support

For questions or issues:
- Check the [documentation](../../docs/)
- Review existing [issues](https://github.com/routstr/routstr/issues)
- Join our community channels
