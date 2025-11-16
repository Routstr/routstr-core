# Payment Method Integration Examples

This document shows how to integrate the modular payment system into the existing codebase.

## Overview

The payment system has been modularized with an abstract `PaymentMethodProvider` interface. Currently implemented:
- **Cashu** (fully functional) - wraps existing wallet logic
- **Lightning** (pseudo) - demonstrates Lightning invoice integration
- **USDT Tether** (pseudo) - demonstrates stablecoin integration  
- **Bitcoin On-Chain** (pseudo) - demonstrates blockchain transaction integration

## Initialization

Add to `routstr/core/main.py` startup:

```python
from routstr.payment import initialize_payment_providers

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # ... existing code ...
    
    # Initialize payment providers
    initialize_payment_providers()
    logger.info("Payment providers initialized")
    
    # ... rest of startup ...
    yield
```

## Usage in Authentication

Update `routstr/auth.py` to use the payment system:

```python
from routstr.payment import (
    PaymentMethodType,
    get_provider_for_token,
    auto_detect_and_redeem,
)

async def validate_bearer_key(
    bearer_key: str,
    session: AsyncSession,
    refund_address: str | None = None,
    key_expiry_time: int | None = None,
) -> ApiKey:
    # ... existing sk- key handling ...
    
    # Auto-detect payment method
    provider = await get_provider_for_token(bearer_key)
    
    if provider:
        logger.debug(
            "Detected payment method",
            extra={
                "method_type": provider.method_type.value,
                "token_preview": bearer_key[:20] + "...",
            },
        )
        
        # Use the provider to redeem
        try:
            amount, unit, source = await provider.redeem_token(bearer_key)
            
            # Rest of the key creation logic...
            # This is compatible with existing Cashu flow
            
        except ValueError as e:
            # Handle redemption errors
            logger.error("Token redemption failed", extra={"error": str(e)})
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": f"Invalid or expired token: {e}",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )
    
    # ... rest of existing validation ...
```

## Alternative: Explicit Provider Selection

For endpoints that want to accept specific payment methods:

```python
from routstr.payment import get_provider_by_type, PaymentMethodType

async def create_balance_with_lightning(
    lightning_invoice: str,
    session: AsyncSession,
) -> dict:
    provider = get_provider_by_type(PaymentMethodType.LIGHTNING)
    
    if not provider:
        raise HTTPException(
            status_code=400,
            detail="Lightning payment method not available"
        )
    
    # Validate invoice format
    if not await provider.validate_token(lightning_invoice):
        raise HTTPException(
            status_code=400,
            detail="Invalid Lightning invoice format"
        )
    
    # Check if paid and credit balance
    try:
        amount_msats, currency, node_pubkey = await provider.redeem_token(
            lightning_invoice
        )
        
        # Create API key with balance...
        
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Lightning payment method not yet implemented"
        )
```

## Refund with Multiple Payment Methods

Update `routstr/balance.py` refund endpoint:

```python
from routstr.payment import get_provider_for_token, PaymentMethodType

@router.post("/refund")
async def refund_wallet_endpoint(
    authorization: Annotated[str, Header(...)],
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    # ... existing validation ...
    
    key: ApiKey = await validate_bearer_key(bearer_value, session)
    remaining_balance_msats: int = key.balance
    
    # Detect original payment method
    provider = await get_provider_for_token(bearer_value)
    
    if not provider:
        # Fallback to Cashu (existing behavior)
        provider = get_provider_by_type(PaymentMethodType.CASHU)
    
    # Use provider for refund
    try:
        refund_details = await provider.create_refund(
            amount=remaining_balance_msats // 1000,  # Convert to base unit
            currency=key.refund_currency or "sat",
            destination=key.refund_address,
        )
        
        result = {}
        if refund_details.token:
            result["token"] = refund_details.token
        if refund_details.destination:
            result["recipient"] = refund_details.destination
        result["msats"] = str(refund_details.amount_msats)
        
        # Delete key and commit
        await session.delete(key)
        await session.commit()
        
        return result
        
    except NotImplementedError as e:
        # Payment method not fully implemented
        raise HTTPException(
            status_code=501,
            detail=f"Refund not available for this payment method: {e}"
        )
```

## Adding New Payment Methods

To add a new payment method:

1. Create a new file in `routstr/payment/` (e.g., `monero_payment.py`)
2. Implement `PaymentMethodProvider` interface
3. Add new `PaymentMethodType` enum value
4. Register in `payment_factory.py`

Example skeleton:

```python
from .payment_method import (
    PaymentMethodProvider,
    PaymentMethodType,
    PaymentToken,
    RefundDetails,
)

class MoneroPaymentProvider(PaymentMethodProvider):
    @property
    def method_type(self) -> PaymentMethodType:
        return PaymentMethodType.MONERO  # Add to enum
    
    async def validate_token(self, token: str) -> bool:
        # Check if Monero transaction ID or payment proof
        pass
    
    async def parse_token(self, token: str) -> PaymentToken:
        # Parse Monero transaction
        pass
    
    async def redeem_token(self, token: str) -> tuple[int, str, str]:
        # Verify transaction on Monero blockchain
        pass
    
    async def create_refund(
        self, amount: int, currency: str, destination: str | None = None
    ) -> RefundDetails:
        # Send Monero transaction
        pass
    
    async def check_balance_sufficiency(
        self, token: str, required_amount_msats: int
    ) -> bool:
        # Check transaction amount
        pass
```

## API Endpoint for Payment Methods

Add to `routstr/balance.py`:

```python
from routstr.payment import list_available_payment_methods

@router.get("/payment-methods")
async def get_payment_methods() -> dict:
    """List all available payment methods."""
    methods = list_available_payment_methods()
    
    return {
        "supported_methods": [method.value for method in methods],
        "default": "cashu",
        "fully_implemented": ["cashu"],
        "coming_soon": ["lightning", "tether", "bitcoin_onchain"],
    }
```

## Testing

Create tests for each payment method:

```python
import pytest
from routstr.payment import get_provider_by_type, PaymentMethodType

@pytest.mark.asyncio
async def test_cashu_provider():
    provider = get_provider_by_type(PaymentMethodType.CASHU)
    assert provider is not None
    
    # Test with real Cashu token
    token = "cashuAey..."
    is_valid = await provider.validate_token(token)
    assert is_valid
    
    payment = await provider.parse_token(token)
    assert payment.amount_msats > 0

@pytest.mark.asyncio
async def test_lightning_provider_not_implemented():
    provider = get_provider_by_type(PaymentMethodType.LIGHTNING)
    assert provider is not None
    
    # Should raise NotImplementedError
    with pytest.raises(NotImplementedError):
        await provider.parse_token("lnbc...")
```

## Migration Strategy

1. **Phase 1** (Current): Keep existing Cashu implementation working
2. **Phase 2**: Update auth.py to use `CashuPaymentProvider` internally
3. **Phase 3**: Add auto-detection for multiple payment methods
4. **Phase 4**: Implement Lightning/Tether providers based on demand
5. **Phase 5**: Fully deprecate direct wallet imports in favor of payment providers
