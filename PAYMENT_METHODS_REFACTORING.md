# Payment Methods Refactoring Summary

## Overview

This document summarizes the refactoring work done to modularize the payment system in Routstr. The system has been redesigned to support multiple payment methods (Cashu, Lightning, USDT, Bitcoin) through an abstract payment method architecture.

## What Changed

### 1. New Payment Method Architecture

**File**: `routstr/payment/methods.py`

A new abstract payment method system was created with:

- **`AbstractPaymentMethod`**: Base class that all payment methods must implement
- **`PaymentCredentials`**: Data class for parsed payment credentials
- **`PaymentResult`**: Data class for payment processing results
- **Payment Method Registry**: Automatic routing to appropriate payment handlers via `get_payment_method()`

### 2. Implemented Payment Methods

#### Cashu (Fully Implemented)
- **Class**: `CashuPaymentMethod`
- **Status**: ✅ Fully functional
- Moved existing Cashu logic into the new payment method pattern
- Still uses `routstr/wallet.py` internally for Cashu operations
- Supports token redemption, multi-mint swapping, and refunds

#### Lightning Network (Pseudo-Implementation)
- **Class**: `LightningPaymentMethod`
- **Status**: 🚧 Interface defined, extensive TODOs for full implementation
- Can detect Lightning invoices (`ln` or `lnbc` prefix)
- Contains detailed implementation comments for:
  - Invoice verification and payment monitoring
  - Lightning node integration
  - LNURL-based refunds
  - Hold invoices (HODL invoices)

#### USDT/Tether (Pseudo-Implementation)
- **Class**: `USDTetherPaymentMethod`
- **Status**: 🚧 Interface defined, extensive TODOs for full implementation
- Can detect USDT transactions (Ethereum tx hash format)
- Contains detailed implementation comments for:
  - Multi-chain support (Ethereum, Tron, Liquid, Lightning)
  - Transaction verification and confirmations
  - Exchange rate conversion (USDT ↔ sats)
  - Gas fee handling for refunds

#### On-Chain Bitcoin (Pseudo-Implementation)
- **Class**: `OnChainBitcoinPaymentMethod`
- **Status**: 🚧 Interface defined, extensive TODOs for full implementation
- Can detect Bitcoin transaction IDs
- Contains detailed implementation comments for:
  - HD wallet address generation
  - Transaction monitoring and confirmations
  - UTXO management for refunds
  - Fee estimation and coin selection

### 3. Updated Core Files

#### `routstr/auth.py`

**Before**:
```python
if bearer_key.startswith("cashu"):
    # Cashu-specific logic hardcoded
    token_obj = deserialize_token_from_string(bearer_key)
    # ... more Cashu-specific code
    msats = await credit_balance(bearer_key, new_key, session)
```

**After**:
```python
try:
    payment_method = get_payment_method(bearer_key)
    payment_result = await payment_method.receive_payment(bearer_key, new_key, session)
    msats = payment_result.amount_msats
except ValueError as ve:
    # No payment method matched, fall through to sk- key validation
```

**Changes**:
- Removed hardcoded Cashu token detection
- Now uses `get_payment_method()` to automatically detect payment type
- Gracefully falls back to legacy `sk-` key validation
- Returns proper HTTP 501 for unimplemented payment methods
- More descriptive logging with payment method names

#### `routstr/balance.py`

**Before**:
```python
from .wallet import credit_balance, send_token, send_to_lnurl

# In topup endpoint
amount_msats = await credit_balance(cashu_token, key, session)

# In refund endpoint
if key.refund_address:
    await send_to_lnurl(...)
else:
    token = await send_token(...)
```

**After**:
```python
from .payment.methods import get_payment_method, CashuPaymentMethod

# In topup endpoint
payment_method = get_payment_method(payment_credential)
payment_result = await payment_method.receive_payment(payment_credential, key, session)
amount_msats = payment_result.amount_msats

# In refund endpoint
payment_method = CashuPaymentMethod()  # Default to Cashu for backward compatibility
result = await payment_method.refund_payment(key, remaining_balance_msats)
```

**Changes**:
- Replaced direct wallet function calls with payment method abstractions
- Topup now supports any registered payment method
- Refund currently defaults to Cashu (backward compatibility)
- Better error handling for unsupported payment methods (HTTP 501)
- More descriptive parameter names (`cashu_token` → `payment_credential`)

#### `routstr/payment/__init__.py`

**Added Exports**:
```python
from .methods import (
    AbstractPaymentMethod,
    CashuPaymentMethod,
    LightningPaymentMethod,
    USDTetherPaymentMethod,
    OnChainBitcoinPaymentMethod,
    PaymentCredentials,
    PaymentResult,
    get_payment_method,
    register_payment_method,
    list_payment_methods,
)
```

### 4. Updated Tests

#### `tests/integration/test_wallet_topup.py`

**Before**:
```python
with patch("routstr.balance.credit_balance") as mock_credit_balance:
    mock_credit_balance.side_effect = Exception("Network error")
```

**After**:
```python
with patch("routstr.payment.methods.CashuPaymentMethod.receive_payment") as mock_receive_payment:
    mock_receive_payment.side_effect = Exception("Network error")
```

**Changes**:
- Updated mock to patch the payment method instead of `credit_balance`
- Test still validates the same behavior, just using the new architecture

### 5. New Documentation

#### `docs/advanced/payment-methods.md`

Comprehensive documentation covering:
- Architecture overview and payment flow
- Detailed implementation status for each payment method
- Step-by-step guides for implementing each pseudo-implemented method
- Creating custom payment methods
- Database schema considerations
- Security best practices
- API endpoint usage examples

Added to `mkdocs.yml` in the Advanced section.

## Backward Compatibility

✅ **Fully backward compatible**

- All existing Cashu functionality preserved
- API endpoints remain unchanged
- Database schema unchanged (no migrations needed yet)
- Existing API keys and balances continue to work
- `sk-` prefixed API keys still supported
- Cashu tokens still work exactly as before

## Migration Path for Developers

### For Users

No action required. The system works exactly as before for Cashu tokens.

### For Developers

If you want to add a new payment method:

1. Create a new class inheriting from `AbstractPaymentMethod`
2. Implement the required methods:
   - `can_handle(credential)`: Detect if this method can process the credential
   - `parse_credential(credential)`: Validate and parse the credential
   - `receive_payment(credential, key, session)`: Process payment and credit balance
   - `refund_payment(key, amount)`: Refund balance to original source
   - `get_refund_metadata(key)`: Extract refund configuration
3. Register your method: `register_payment_method(YourPaymentMethod())`
4. Optionally update `ApiKey` model to store method-specific metadata

See `docs/advanced/payment-methods.md` for detailed implementation guides.

## Implementation Status

| Payment Method | Detection | Parse | Receive | Refund | Status |
|---------------|-----------|-------|---------|--------|--------|
| Cashu eCash | ✅ | ✅ | ✅ | ✅ | **Production Ready** |
| Lightning | ✅ | 🚧 | 🚧 | 🚧 | Pseudo-implemented |
| USDT | ✅ | 🚧 | 🚧 | 🚧 | Pseudo-implemented |
| Bitcoin | ✅ | 🚧 | 🚧 | 🚧 | Pseudo-implemented |

Legend:
- ✅ Fully implemented
- 🚧 Interface defined with TODO comments

## Next Steps

### For Lightning Implementation

1. Add Lightning library dependency (`lnbits-client`, `lnd-grpc`, or `c-lightning-python`)
2. Configure Lightning node connection in `core/settings.py`
3. Implement invoice decoding and payment verification
4. Add webhook/polling for invoice payments
5. Implement LNURL-based refunds
6. Update `ApiKey` model with `lightning_payment_hash` and `lightning_preimage`

### For USDT Implementation

1. Choose blockchain network (Ethereum, Tron, Liquid, Lightning)
2. Add blockchain library (`web3.py` for Ethereum, `tronpy` for Tron)
3. Configure blockchain node/API connection
4. Implement transaction verification with confirmation waiting
5. Add exchange rate API integration (USDT ↔ sats)
6. Implement gas-paying refund logic
7. Update `ApiKey` model with `usdt_chain`, `usdt_tx_hash`, `usdt_sender_address`

### For On-Chain Bitcoin Implementation

1. Add Bitcoin library (`bitcoinlib` or `bitcoin-python`)
2. Configure Bitcoin node RPC connection
3. Implement HD wallet for unique deposit addresses
4. Add transaction monitoring and confirmation tracking
5. Implement UTXO management and coin selection
6. Add fee estimation for refunds
7. Update `ApiKey` model with `btc_deposit_address`, `btc_deposit_txid`, `btc_address_index`

### For All Methods

1. Add `payment_method_type` field to `ApiKey` model (optional but recommended)
2. Create database migration for new fields
3. Update refund logic to auto-detect payment method from `ApiKey`
4. Add comprehensive integration tests
5. Update UI to support multiple payment methods

## Testing

All existing tests pass:
- Unit tests in `tests/unit/test_wallet.py` still work (Cashu-specific)
- Integration tests in `tests/integration/test_wallet_topup.py` updated and passing
- No new tests added yet for pseudo-implemented methods

To test new payment methods as they're implemented:
1. Create unit tests for each payment method class
2. Create integration tests that use real/mocked services
3. Test concurrent payment processing
4. Test refund flows
5. Test error handling and edge cases

## Files Modified

### Created
- `routstr/payment/methods.py` (580 lines)
- `docs/advanced/payment-methods.md` (comprehensive guide)
- `PAYMENT_METHODS_REFACTORING.md` (this file)

### Modified
- `routstr/auth.py` (refactored payment credential handling)
- `routstr/balance.py` (refactored topup and refund endpoints)
- `routstr/payment/__init__.py` (added exports)
- `tests/integration/test_wallet_topup.py` (updated one mock)
- `mkdocs.yml` (added payment-methods.md to nav)

### Unchanged (Still Used by CashuPaymentMethod)
- `routstr/wallet.py` (Cashu-specific logic)
- `routstr/core/db.py` (ApiKey model)
- `routstr/core/settings.py` (configuration)
- All other test files

## Security Considerations

The new architecture maintains all existing security measures:
- Atomic SQL updates prevent race conditions
- Payment credentials are hashed before storage
- Idempotency prevents double-processing (via Cashu token tracking)
- Private keys never logged or exposed
- All payment methods must implement proper validation

Additional security considerations for new methods are documented in `docs/advanced/payment-methods.md`.

## Performance Impact

✅ **No performance degradation**

- Payment method detection is O(n) where n is number of registered methods (currently 4)
- Cashu payments use the same code path as before (just wrapped in a class)
- No additional database queries
- No additional external API calls

## Future Considerations

1. **Multi-Currency Support**: System is designed to support any currency via exchange rate conversion
2. **Payment Method Selection**: Could add UI for users to choose preferred payment method
3. **Hybrid Payments**: Could support splitting payment across multiple methods
4. **Payment History**: Consider adding a `payments` table to track all transactions
5. **Automated Testing**: Add tests that run against real payment networks (testnet/mainnet)
6. **Payment Method Marketplace**: Allow third-party payment method plugins
7. **WebSocket Notifications**: Real-time payment confirmations for better UX

## Questions or Issues?

- See `docs/advanced/payment-methods.md` for implementation guides
- Review code in `routstr/payment/methods.py` for detailed TODO comments
- Check existing Cashu implementation as a reference
- Open an issue on GitHub for questions or suggestions

## Credits

This refactoring enables Routstr to support multiple payment methods while maintaining full backward compatibility with existing Cashu-based payments. The architecture is designed to make adding new payment methods straightforward and consistent.
