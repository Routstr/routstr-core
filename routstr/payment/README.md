# Modular Payment System for Temporary Balances

This directory contains a modular payment system that abstracts payment method orchestration for temporary balance management in Routstr.

## Architecture

The system is built around the `PaymentMethodProvider` abstract base class, which defines a standard interface for different payment methods:

```
payment/
├── payment_method.py          # Abstract base class and registry
├── payment_factory.py         # Initialization and convenience functions
├── cashu_payment.py           # Cashu eCash implementation (FUNCTIONAL)
├── lightning_payment.py       # Lightning Network implementation (PSEUDO)
├── tether_payment.py          # USDT Tether implementation (PSEUDO)
├── bitcoin_onchain_payment.py # Bitcoin on-chain implementation (PSEUDO)
├── INTEGRATION_EXAMPLE.md     # Integration examples
└── README.md                  # This file
```

## Supported Payment Methods

| Method | Status | Token Format | Notes |
|--------|--------|--------------|-------|
| **Cashu** | ✅ Fully Functional | `cashuAey...` | Wraps existing wallet logic |
| **Lightning** | 🚧 Pseudo | `lnbc...` | Requires Lightning node integration |
| **USDT Tether** | 🚧 Pseudo | `0x...` or `usdt:...` | Requires blockchain integration |
| **Bitcoin On-Chain** | 🚧 Pseudo | 64-char txid or `btc:...` | Requires Bitcoin node integration |

## Key Components

### 1. PaymentMethodProvider (Abstract Base Class)

All payment methods implement this interface:

```python
class PaymentMethodProvider(ABC):
    @property
    @abstractmethod
    def method_type(self) -> PaymentMethodType:
        """Return payment method identifier"""
        
    @abstractmethod
    async def validate_token(self, token: str) -> bool:
        """Check if token belongs to this payment method"""
        
    @abstractmethod
    async def parse_token(self, token: str) -> PaymentToken:
        """Parse token into structured data"""
        
    @abstractmethod
    async def redeem_token(self, token: str) -> tuple[int, str, str]:
        """Redeem token and credit balance"""
        
    @abstractmethod
    async def create_refund(
        self, amount: int, currency: str, destination: str | None = None
    ) -> RefundDetails:
        """Create refund payment"""
        
    @abstractmethod
    async def check_balance_sufficiency(
        self, token: str, required_amount_msats: int
    ) -> bool:
        """Check if token has sufficient balance"""
```

### 2. PaymentMethodRegistry

Central registry for managing multiple payment providers:

```python
registry = get_payment_registry()

# Register providers
registry.register(CashuPaymentProvider())
registry.register(LightningPaymentProvider())

# Auto-detect provider for a token
provider = await registry.detect_provider(token)

# Get specific provider
cashu = registry.get_provider(PaymentMethodType.CASHU)
```

### 3. Factory Functions

Convenience functions for common operations:

```python
from routstr.payment import (
    initialize_payment_providers,
    get_provider_for_token,
    auto_detect_and_redeem,
)

# Initialize all providers (call at startup)
initialize_payment_providers()

# Auto-detect and redeem any supported token
amount, currency, source = await auto_detect_and_redeem(token)
```

## Usage Examples

### Basic Token Redemption

```python
from routstr.payment import auto_detect_and_redeem

try:
    amount, currency, source = await auto_detect_and_redeem(token)
    print(f"Redeemed {amount} {currency} from {source}")
except ValueError as e:
    print(f"Failed: {e}")
```

### Specific Provider Usage

```python
from routstr.payment import get_provider_by_type, PaymentMethodType

# Get Cashu provider
cashu = get_provider_by_type(PaymentMethodType.CASHU)

# Validate token format
if await cashu.validate_token(token):
    # Parse token details
    payment = await cashu.parse_token(token)
    print(f"Amount: {payment.amount_msats} msats")
    
    # Redeem token
    amount, unit, mint = await cashu.redeem_token(token)
```

### Create Refund

```python
from routstr.payment import get_provider_by_type, PaymentMethodType

provider = get_provider_by_type(PaymentMethodType.CASHU)

# Refund to LNURL
refund = await provider.create_refund(
    amount=1000,  # sats
    currency="sat",
    destination="lnurl1..."
)

# Or create token for user to claim
refund = await provider.create_refund(
    amount=1000,
    currency="sat",
    destination=None  # Returns token instead
)

print(f"Refund token: {refund.token}")
```

## Implementation Status

### ✅ Cashu (Fully Functional)

The Cashu payment provider wraps the existing wallet logic from `routstr/wallet.py`:

- ✅ Token validation and parsing
- ✅ Token redemption via `recieve_token()`
- ✅ Refund creation via `send_token()` and `send_to_lnurl()`
- ✅ Balance sufficiency checks
- ✅ Multi-mint support
- ✅ Auto-swap to primary mint

### 🚧 Lightning (Pseudo Implementation)

To fully implement Lightning payments, you need:

1. **Lightning Node Integration**
   - LND, Core Lightning (CLN), or Eclair
   - Libraries: `python-lnd-grpc`, `pylightning`, `bolt11`

2. **Invoice Management**
   - Generate invoices with unique payment hashes
   - Monitor payment status (webhook or polling)
   - Handle invoice expiry (typically 3600s)

3. **Payment Processing**
   - Lookup invoice status by payment hash
   - Verify settlement before crediting
   - Store payment_hash -> api_key mapping

4. **Refund Mechanism**
   - Pay Lightning invoices programmatically
   - Implement keysend for no-invoice refunds
   - Handle routing failures and retries

See `lightning_payment.py` for detailed implementation comments.

### 🚧 USDT Tether (Pseudo Implementation)

To fully implement USDT payments, you need:

1. **Blockchain Integration**
   - Support Ethereum (ERC-20), Tron (TRC-20), or Liquid
   - Libraries: `web3.py`, `tronpy`, or `elements`
   - Node access via Infura, Alchemy, or self-hosted

2. **Wallet Management**
   - Generate unique deposit addresses (HD wallet)
   - Monitor for incoming transactions
   - Wait for confirmations (6+ for Ethereum)

3. **Exchange Rate Integration**
   - Convert USDT to satoshis
   - Use price oracles (Chainlink, CoinGecko)
   - Handle depeg scenarios

4. **Transaction Broadcasting**
   - Build and sign USDT transfers
   - Estimate and pay gas fees
   - Handle transaction failures

See `tether_payment.py` for detailed implementation comments.

### 🚧 Bitcoin On-Chain (Pseudo Implementation)

To fully implement Bitcoin on-chain payments, you need:

1. **Bitcoin Node Integration**
   - Bitcoin Core RPC connection
   - Libraries: `python-bitcoinlib`, `bitcoinrpc`
   - Or block explorer APIs (BlockCypher, Blockchain.com)

2. **Address Management**
   - Generate HD wallet addresses (BIP32/BIP44)
   - Support P2PKH, P2WPKH, P2TR address types
   - Track address derivation paths

3. **Transaction Monitoring**
   - Poll mempool for incoming transactions
   - Track confirmations (1-6 required)
   - Handle chain reorganizations

4. **UTXO Management**
   - Track unspent outputs for refunds
   - Implement coin selection algorithms
   - Handle UTXO consolidation

5. **Fee Management**
   - Dynamic fee estimation from mempool
   - Support RBF (Replace By Fee)
   - Handle fee bumping for stuck transactions

See `bitcoin_onchain_payment.py` for detailed implementation comments.

## Adding New Payment Methods

To add a new payment method (e.g., Monero, Zcash, Stellar):

1. **Create provider file** (`routstr/payment/your_method_payment.py`)

```python
from .payment_method import (
    PaymentMethodProvider,
    PaymentMethodType,
    PaymentToken,
    RefundDetails,
)

class YourMethodPaymentProvider(PaymentMethodProvider):
    @property
    def method_type(self) -> PaymentMethodType:
        return PaymentMethodType.YOUR_METHOD  # Add to enum
    
    # Implement all abstract methods
    async def validate_token(self, token: str) -> bool:
        # Your implementation
        pass
    
    # ... etc
```

2. **Add enum value** to `PaymentMethodType` in `payment_method.py`

```python
class PaymentMethodType(str, Enum):
    CASHU = "cashu"
    LIGHTNING = "lightning"
    TETHER = "tether"
    BITCOIN_ONCHAIN = "bitcoin_onchain"
    YOUR_METHOD = "your_method"  # Add this
```

3. **Register in factory** (`payment_factory.py`)

```python
def initialize_payment_providers() -> PaymentMethodRegistry:
    # ... existing registrations ...
    
    your_provider = YourMethodPaymentProvider()
    registry.register(your_provider)
    logger.info("Registered Your Method payment provider")
```

4. **Test the implementation**

```python
import pytest
from routstr.payment import get_provider_by_type, PaymentMethodType

@pytest.mark.asyncio
async def test_your_method():
    provider = get_provider_by_type(PaymentMethodType.YOUR_METHOD)
    assert provider is not None
    
    token = "your_token_format..."
    is_valid = await provider.validate_token(token)
    assert is_valid
```

## Integration with Existing Code

The payment system is designed to be backward-compatible with existing Cashu-specific code. See `INTEGRATION_EXAMPLE.md` for detailed migration examples.

### Quick Integration

Add to application startup:

```python
from routstr.payment import initialize_payment_providers

@app.on_event("startup")
async def startup():
    initialize_payment_providers()
```

Use in authentication:

```python
from routstr.payment import get_provider_for_token

async def validate_bearer_key(bearer_key: str, session: AsyncSession) -> ApiKey:
    provider = await get_provider_for_token(bearer_key)
    
    if provider:
        amount, unit, source = await provider.redeem_token(bearer_key)
        # Create API key with balance...
```

## Design Principles

1. **Abstraction**: Common interface for all payment methods
2. **Modularity**: Each payment method is self-contained
3. **Extensibility**: Easy to add new payment methods
4. **Backward Compatibility**: Existing Cashu flow continues to work
5. **Type Safety**: Full type hints for all methods
6. **Async First**: All I/O operations are async
7. **Error Handling**: Consistent error semantics across providers

## Future Enhancements

- [ ] Implement Lightning payment method
- [ ] Implement USDT Tether payment method  
- [ ] Implement Bitcoin on-chain payment method
- [ ] Add payment method preferences per user
- [ ] Support mixed payment methods for single balance
- [ ] Add payment method analytics/metrics
- [ ] Implement automatic currency conversion
- [ ] Add webhook support for async payment confirmation
- [ ] Support for additional stablecoins (USDC, DAI)
- [ ] Support for other cryptocurrencies (Monero, Zcash)

## Security Considerations

When implementing new payment methods, ensure:

- ✅ Token validation prevents injection attacks
- ✅ Private keys stored securely (HSM, encrypted storage)
- ✅ Double-spend prevention (transaction tracking)
- ✅ Rate limiting on redemption attempts
- ✅ Minimum balance requirements (cover network fees)
- ✅ Address validation before sending refunds
- ✅ Transaction confirmation requirements met
- ✅ Proper error handling (no information leakage)
- ✅ Logging for audit trails

## License

Same as parent project (see LICENSE in repository root).
