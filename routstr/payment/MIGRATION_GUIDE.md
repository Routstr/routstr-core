# Migration Guide: Transitioning to Modular Payment System

This guide provides step-by-step instructions for gradually migrating from the existing Cashu-specific implementation to the new modular payment system.

## Overview

The migration can be done gradually without breaking existing functionality. The new system is designed to be backward-compatible.

## Migration Phases

### Phase 1: Initialize the System (No Breaking Changes)

Add payment provider initialization to your application startup.

**File: `routstr/core/main.py`**

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from routstr.payment import initialize_payment_providers

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Existing startup code...
    
    # Add this line to initialize payment providers
    initialize_payment_providers()
    logger.info("Payment providers initialized")
    
    # Existing startup code continues...
    yield
    
    # Existing shutdown code...

app = FastAPI(lifespan=lifespan)
```

✅ **Result**: Payment providers are initialized, but nothing changes in behavior.

### Phase 2: Add Migration Helpers (Optional, Recommended)

Use migration helpers as drop-in replacements to test the new system.

**File: `routstr/wallet.py`** (Example modification)

```python
# At the top of the file, add:
from .payment.migration_helper import credit_balance_via_provider as credit_balance_new

# Then you can optionally use the new implementation:
async def credit_balance(
    cashu_token: str, key: db.ApiKey, session: db.AsyncSession
) -> int:
    """
    Original implementation - can be gradually replaced with credit_balance_new
    """
    # Option 1: Keep original implementation (safe)
    # ... existing code ...
    
    # Option 2: Use new implementation (test first!)
    # return await credit_balance_new(cashu_token, key, session)
```

✅ **Result**: You can A/B test the new implementation without committing to it.

### Phase 3: Update Authentication (Backward Compatible)

Enhance auth.py to auto-detect payment methods while keeping Cashu working.

**File: `routstr/auth.py`**

```python
from routstr.payment import get_provider_for_token

async def validate_bearer_key(
    bearer_key: str,
    session: AsyncSession,
    refund_address: str | None = None,
    key_expiry_time: int | None = None,
) -> ApiKey:
    # ... existing sk- key handling ...
    
    # Try auto-detection first (new system)
    provider = await get_provider_for_token(bearer_key)
    
    if provider:
        logger.debug(
            "Using modular payment system",
            extra={"method": provider.method_type.value}
        )
        
        try:
            # Use provider for redemption
            hashed_key = hashlib.sha256(bearer_key.encode()).hexdigest()
            token_obj = deserialize_token_from_string(bearer_key)
            
            # ... rest of existing key creation logic ...
            
            # Use provider to redeem
            amount, unit, mint_url = await provider.redeem_token(bearer_key)
            
            # ... rest of existing balance update logic ...
            
        except Exception as e:
            logger.warning(
                "Provider redemption failed, falling back to legacy",
                extra={"error": str(e)}
            )
            # Fall through to legacy Cashu handling below
    
    # Legacy Cashu handling (fallback)
    if bearer_key.startswith("cashu"):
        # ... existing Cashu-specific code ...
        pass
    
    # ... rest of existing validation ...
```

✅ **Result**: New payment methods can be detected, but existing Cashu flow is preserved.

### Phase 4: Update Balance Endpoints (Backward Compatible)

Add support for multiple payment methods in topup and refund.

**File: `routstr/balance.py`**

#### Topup Endpoint

```python
from routstr.payment import get_provider_for_token

@router.post("/topup")
async def topup_wallet_endpoint(
    cashu_token: str | None = None,
    topup_request: TopupRequest | None = None,
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    if topup_request is not None:
        cashu_token = topup_request.cashu_token
    if cashu_token is None:
        raise HTTPException(status_code=400, detail="A token is required.")

    token = cashu_token.replace("\n", "").replace("\r", "").replace("\t", "")
    
    # Try new modular system first
    provider = await get_provider_for_token(token)
    
    if provider:
        try:
            amount, unit, _ = await provider.redeem_token(token)
            
            # Convert to msats
            amount_msats = amount * 1000 if unit == "sat" else amount
            
            # Update balance (existing logic)
            from sqlmodel import col, update
            stmt = (
                update(ApiKey)
                .where(col(ApiKey.hashed_key) == key.hashed_key)
                .values(balance=(ApiKey.balance) + amount_msats)
            )
            await session.exec(stmt)
            await session.commit()
            
            return {"msats": amount_msats}
            
        except ValueError as e:
            # Handle provider errors
            error_msg = str(e)
            if "already spent" in error_msg.lower():
                raise HTTPException(status_code=400, detail="Token already spent")
            elif "invalid" in error_msg.lower():
                raise HTTPException(status_code=400, detail="Invalid token format")
            else:
                raise HTTPException(status_code=400, detail="Failed to redeem token")
    
    # Fallback to existing implementation
    try:
        amount_msats = await credit_balance(token, key, session)
        return {"msats": amount_msats}
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")
```

#### Refund Endpoint

```python
from routstr.payment import get_provider_for_token, get_provider_by_type, PaymentMethodType

@router.post("/refund")
async def refund_wallet_endpoint(
    authorization: Annotated[str, Header(...)],
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    # ... existing validation ...
    
    key: ApiKey = await validate_bearer_key(bearer_value, session)
    remaining_balance_msats: int = key.balance
    
    # Detect payment method from original token
    provider = await get_provider_for_token(bearer_value)
    
    # Fallback to Cashu if auto-detection fails
    if not provider:
        provider = get_provider_by_type(PaymentMethodType.CASHU)
    
    if not provider:
        raise HTTPException(status_code=500, detail="No payment provider available")
    
    # Prepare refund
    if key.refund_currency == "sat":
        refund_amount = remaining_balance_msats // 1000
    else:
        refund_amount = remaining_balance_msats
    
    try:
        # Use provider to create refund
        refund_details = await provider.create_refund(
            amount=refund_amount,
            currency=key.refund_currency or "sat",
            destination=key.refund_address,
        )
        
        # Build result
        result = {}
        if refund_details.token:
            result["token"] = refund_details.token
        if refund_details.destination:
            result["recipient"] = refund_details.destination
        
        if key.refund_currency == "sat":
            result["sats"] = str(refund_amount)
        else:
            result["msats"] = str(remaining_balance_msats)
        
        # Cache and cleanup
        await _refund_cache_set(bearer_value, result)
        await session.delete(key)
        await session.commit()
        
        return result
        
    except NotImplementedError:
        # Payment method not fully implemented, fallback
        raise HTTPException(
            status_code=501,
            detail="Refund not available for this payment method"
        )
    except Exception as e:
        # Handle errors
        raise HTTPException(status_code=500, detail="Refund failed")
```

✅ **Result**: Multiple payment methods supported, existing Cashu flow preserved.

### Phase 5: Add Payment Methods Endpoint (New Feature)

Add an endpoint to list supported payment methods.

**File: `routstr/balance.py`**

```python
from routstr.payment import list_available_payment_methods

@router.get("/payment-methods")
async def list_payment_methods() -> dict:
    """
    List all available payment methods.
    
    Returns:
        {
            "supported_methods": ["cashu", "lightning", "tether", ...],
            "default": "cashu",
            "fully_implemented": ["cashu"],
            "coming_soon": ["lightning", "tether", "bitcoin_onchain"]
        }
    """
    methods = list_available_payment_methods()
    
    return {
        "supported_methods": [m.value for m in methods],
        "default": "cashu",
        "fully_implemented": ["cashu"],
        "coming_soon": [
            m.value for m in methods 
            if m.value != "cashu"
        ],
    }
```

✅ **Result**: Clients can discover available payment methods.

### Phase 6: Update Frontend (Optional)

Add UI to support multiple payment methods.

```typescript
// Example: Fetch available payment methods
const paymentMethods = await fetch('/v1/balance/payment-methods')
  .then(r => r.json());

console.log('Available:', paymentMethods.supported_methods);
console.log('Coming soon:', paymentMethods.coming_soon);

// Show appropriate input based on selected method
if (selectedMethod === 'cashu') {
  // Show Cashu token input
} else if (selectedMethod === 'lightning') {
  // Show Lightning invoice input
} else if (selectedMethod === 'tether') {
  // Show USDT transaction ID input
}
```

✅ **Result**: Users can choose their preferred payment method.

### Phase 7: Implement Additional Payment Methods (As Needed)

When ready to support Lightning, Tether, or Bitcoin on-chain:

1. Follow the implementation requirements in the respective pseudo-implementation files
2. Update the provider from pseudo to functional
3. Test thoroughly with small amounts
4. Enable in production

✅ **Result**: Full multi-payment-method support.

## Testing Strategy

### 1. Unit Tests

```python
import pytest
from routstr.payment import (
    get_provider_by_type,
    PaymentMethodType,
    initialize_payment_providers,
)

@pytest.fixture(scope="session", autouse=True)
def init_providers():
    initialize_payment_providers()

@pytest.mark.asyncio
async def test_cashu_provider_available():
    provider = get_provider_by_type(PaymentMethodType.CASHU)
    assert provider is not None
    assert provider.method_type == PaymentMethodType.CASHU

@pytest.mark.asyncio
async def test_cashu_token_validation():
    provider = get_provider_by_type(PaymentMethodType.CASHU)
    
    # Valid Cashu token
    assert await provider.validate_token("cashuAey...")
    
    # Invalid tokens
    assert not await provider.validate_token("invalid")
    assert not await provider.validate_token("lnbc...")  # Lightning
```

### 2. Integration Tests

```python
@pytest.mark.asyncio
async def test_topup_with_cashu_via_provider(integration_client):
    # Create token
    token = generate_test_token(amount=1000)
    
    # Topup using new system
    response = integration_client.post(
        "/v1/wallet/topup",
        params={"cashu_token": token}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["msats"] == 1000000
```

### 3. Backward Compatibility Tests

```python
@pytest.mark.asyncio
async def test_existing_cashu_flow_still_works(integration_client):
    """Ensure existing Cashu flow is not broken."""
    
    # This test should pass with both old and new implementation
    token = generate_test_token(amount=500)
    
    # Create balance
    response = integration_client.get(
        "/create",
        params={"initial_balance_token": token}
    )
    
    assert response.status_code == 200
    api_key = response.json()["api_key"]
    
    # Use balance
    response = integration_client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
    )
    
    assert response.status_code == 200
```

## Rollback Plan

If issues arise during migration:

1. **Phase 1-2**: Simply remove initialization call
2. **Phase 3-4**: Remove auto-detection code, keep only legacy paths
3. **Phase 5+**: Remove new endpoints, revert to original implementation

All phases are designed to be non-breaking and reversible.

## Performance Considerations

The new system has minimal overhead:

- **Auto-detection**: O(n) where n = number of providers (currently 4)
- **Provider lookup**: O(1) dictionary lookup
- **Validation**: Same as before (token parsing)
- **Redemption**: Identical to existing implementation for Cashu

Expected performance impact: < 1ms per request.

## Migration Checklist

- [ ] Phase 1: Initialize providers at startup
- [ ] Phase 2: Test with migration helpers
- [ ] Phase 3: Update authentication to use auto-detection
- [ ] Phase 4: Update balance endpoints
- [ ] Phase 5: Add payment methods list endpoint
- [ ] Phase 6: Update frontend (optional)
- [ ] Phase 7: Implement additional methods (optional)

## Getting Help

- See `README.md` for system documentation
- See `INTEGRATION_EXAMPLE.md` for code examples
- See `usage_example.py` for runnable examples
- Check pseudo-implementation files for requirements

## Success Criteria

✅ Existing Cashu functionality works unchanged
✅ New payment providers can be detected and used
✅ System is extensible for future payment methods
✅ No breaking changes to existing APIs
✅ Performance is maintained
✅ All tests pass
