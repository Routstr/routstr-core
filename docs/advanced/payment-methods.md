# Payment Methods

Routstr supports multiple payment methods for funding temporary API key balances. This document describes the payment method architecture and how to add new payment methods.

## Overview

The payment method system is built on an abstract base class that defines a consistent interface for:
- Detecting valid payment credentials
- Receiving and crediting payments
- Refunding balances back to the original payment source
- Extracting refund-related metadata

## Architecture

### Core Components

1. **`AbstractPaymentMethod`**: Abstract base class that all payment methods must implement
2. **`PaymentCredentials`**: Data class containing parsed credential information
3. **`PaymentResult`**: Data class containing payment processing results
4. **Payment Method Registry**: Automatic detection and routing to appropriate payment handlers

### Payment Flow

```
User provides credential (token/invoice/tx)
    ↓
get_payment_method() detects appropriate handler
    ↓
parse_credential() validates format
    ↓
receive_payment() processes and credits balance
    ↓
refund_payment() returns funds when requested
```

## Implemented Payment Methods

### 1. Cashu eCash (Fully Implemented)

**Status**: ✅ Fully functional

**Credentials**: Starts with `cashu`

**Implementation**: `CashuPaymentMethod`

**Features**:
- Token redemption from multiple mints
- Automatic mint detection and swapping
- Lightning address refunds via LNURL
- Cashu token refunds
- Multi-currency support (sats, msats)

**Refund Options**:
- Lightning address (via LNURL)
- New Cashu token

### 2. Bitcoin Lightning Network (Pseudo-Implementation)

**Status**: 🚧 Interface defined, implementation pending

**Credentials**: Starts with `ln` or `lnbc` (Lightning invoices)

**Implementation**: `LightningPaymentMethod`

**To Fully Implement**:

1. **Add Lightning Library Dependency**
   ```toml
   # In pyproject.toml
   dependencies = [
       "lnbits-client>=0.1.0",  # or lnd-grpc, c-lightning-python
   ]
   ```

2. **Configure Lightning Node**
   ```python
   # In core/settings.py
   lightning_host: str = Field(default="localhost:10009")
   lightning_macaroon: str = Field(default="")
   lightning_cert_path: str | None = None
   ```

3. **Update ApiKey Model**
   ```python
   # Add to ApiKey in core/db.py
   lightning_payment_hash: str | None = None
   lightning_preimage: str | None = None
   ```

4. **Implement Invoice Verification**
   ```python
   async def receive_payment(self, credential: str, key: ApiKey, session: AsyncSession):
       node = LightningNode(host=settings.lightning_host)
       invoice = await node.decode_invoice(credential)
       
       # Wait for payment
       payment = await node.lookup_invoice(invoice.payment_hash)
       if not payment.settled:
           raise ValueError("Invoice not yet paid")
       
       # Credit balance
       amount_msats = payment.amount_msat
       # ... (atomic balance update)
       
       return PaymentResult(
           amount_msats=amount_msats,
           currency="btc",
           payment_method="lightning",
           transaction_id=invoice.payment_hash,
       )
   ```

5. **Implement Refunds**
   - Fetch LNURL data from lightning address
   - Request invoice from LNURL service
   - Pay invoice using lightning node
   - Return payment hash and preimage

6. **Add Webhook/Polling**
   - Monitor invoice payments in background
   - Update balance automatically when paid

### 3. USDT (Tether) Stablecoin (Pseudo-Implementation)

**Status**: 🚧 Interface defined, implementation pending

**Credentials**: Starts with `0x` (Ethereum tx), `usdt:`, or `tether:`

**Implementation**: `USDTetherPaymentMethod`

**To Fully Implement**:

1. **Choose Blockchain Network**
   - Ethereum (ERC-20)
   - Tron (TRC-20)
   - Liquid Network
   - Lightning Network (Taproot Assets/RGB)

2. **Add Blockchain Library**
   ```toml
   # For Ethereum/ERC-20
   dependencies = [
       "web3>=6.0.0",
   ]
   ```

3. **Configure Blockchain Node/API**
   ```python
   # In core/settings.py
   ethereum_rpc_url: str = Field(default="https://mainnet.infura.io/v3/...")
   usdt_contract_address: str = Field(default="0xdAC17F958D2ee523a2206206994597C13D831ec7")
   usdt_receiving_address: str = Field(default="")
   usdt_hot_wallet_address: str = Field(default="")
   usdt_private_key: str = Field(default="")
   usdt_required_confirmations: int = Field(default=3)
   ```

4. **Update ApiKey Model**
   ```python
   # Add to ApiKey in core/db.py
   usdt_chain: str | None = None  # "ethereum", "tron", etc.
   usdt_tx_hash: str | None = None
   usdt_sender_address: str | None = None
   ```

5. **Implement Transaction Verification**
   ```python
   async def receive_payment(self, credential: str, key: ApiKey, session: AsyncSession):
       w3 = Web3(Web3.HTTPProvider(settings.ethereum_rpc_url))
       tx_receipt = w3.eth.get_transaction_receipt(credential)
       
       # Verify USDT transfer
       # Parse Transfer event from logs
       amount_usdt = self._decode_transfer_amount(tx_receipt)
       
       # Wait for confirmations
       current_block = w3.eth.block_number
       confirmations = current_block - tx_receipt.blockNumber
       if confirmations < settings.usdt_required_confirmations:
           raise ValueError(f"Insufficient confirmations: {confirmations}")
       
       # Convert USDT to msats using exchange rate
       rate = await get_usdt_to_sats_rate()
       amount_sats = int(amount_usdt * rate)
       amount_msats = amount_sats * 1000
       
       # Credit balance atomically
       # ... (atomic balance update)
       
       return PaymentResult(
           amount_msats=amount_msats,
           currency="usdt",
           payment_method="usdt",
           transaction_id=credential,
       )
   ```

6. **Implement Refunds**
   - Estimate gas fees
   - Build USDT transfer transaction
   - Sign with hot wallet
   - Broadcast to network
   - Handle gas payment

7. **Add Exchange Rate Service**
   ```python
   async def get_usdt_to_sats_rate() -> float:
       # Use price API (CoinGecko, Binance, etc.)
       # Return rate: 1 USDT = X sats
       pass
   ```

8. **Monitor Blockchain Events**
   - Set up event listener for incoming USDT
   - Auto-credit balances on confirmation

### 4. On-Chain Bitcoin (Pseudo-Implementation)

**Status**: 🚧 Interface defined, implementation pending

**Credentials**: 64-character hex (txid), `bitcoin:`, or `btc:`

**Implementation**: `OnChainBitcoinPaymentMethod`

**To Fully Implement**:

1. **Add Bitcoin Library**
   ```toml
   dependencies = [
       "bitcoinlib>=0.6.0",
       # or "bitcoin-python>=0.7.0"
   ]
   ```

2. **Configure Bitcoin Node**
   ```python
   # In core/settings.py
   bitcoin_rpc_url: str = Field(default="http://user:pass@localhost:8332")
   btc_receiving_address: str = Field(default="")
   btc_hot_wallet_address: str = Field(default="")
   btc_change_address: str = Field(default="")
   btc_required_confirmations: int = Field(default=3)
   ```

3. **Implement HD Wallet**
   - Generate unique deposit address per ApiKey
   - Store derivation path in database

4. **Update ApiKey Model**
   ```python
   # Add to ApiKey in core/db.py
   btc_deposit_address: str | None = None
   btc_deposit_txid: str | None = None
   btc_address_index: int | None = None
   ```

5. **Implement Transaction Monitoring**
   - Poll for incoming transactions
   - Verify confirmations
   - Prevent double-processing

6. **Implement Refunds**
   - UTXO management
   - Coin selection algorithm
   - Fee estimation
   - Transaction building and signing
   - Broadcasting

## Creating a Custom Payment Method

### Step 1: Implement the Abstract Class

```python
from routstr.payment.methods import AbstractPaymentMethod, PaymentCredentials, PaymentResult
from routstr.core.db import ApiKey, AsyncSession

class MyCustomPaymentMethod(AbstractPaymentMethod):
    def can_handle(self, credential: str) -> bool:
        """Return True if this method can process the credential."""
        return credential.startswith("mycustom:")
    
    async def parse_credential(self, credential: str) -> PaymentCredentials:
        """Parse and validate the credential."""
        # Parse credential format
        # Extract metadata
        return PaymentCredentials(
            raw_credential=credential,
            payment_type="mycustom",
            metadata={"amount": 1000, "id": "..."},
        )
    
    async def receive_payment(
        self, credential: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        """Process the payment and credit the balance."""
        # 1. Verify payment is valid
        # 2. Calculate amount in msats
        # 3. Credit balance atomically
        
        from sqlmodel import col, update
        
        amount_msats = 1000000  # Your calculation
        
        stmt = (
            update(ApiKey)
            .where(col(ApiKey.hashed_key) == key.hashed_key)
            .values(balance=ApiKey.balance + amount_msats)
        )
        await session.exec(stmt)
        await session.commit()
        await session.refresh(key)
        
        return PaymentResult(
            amount_msats=amount_msats,
            currency="custom",
            payment_method="mycustom",
            transaction_id="...",
        )
    
    async def refund_payment(
        self, key: ApiKey, amount_msats: int | None = None
    ) -> dict[str, str]:
        """Refund the balance."""
        # 1. Calculate refund amount
        # 2. Send refund via your payment system
        # 3. Return details
        
        return {
            "transaction_id": "...",
            "recipient": key.refund_address or "",
            "amount_msats": str(amount_msats or key.balance),
            "method": "mycustom",
        }
    
    def get_refund_metadata(self, key: ApiKey) -> dict[str, str]:
        """Extract refund metadata from ApiKey."""
        return {
            "refund_address": key.refund_address or "",
            "payment_method": "mycustom",
        }
```

### Step 2: Register the Payment Method

```python
from routstr.payment.methods import register_payment_method

# In your initialization code (e.g., core/main.py startup)
register_payment_method(MyCustomPaymentMethod())
```

### Step 3: Configure Settings (if needed)

```python
# In core/settings.py
class Settings(BaseSettings):
    # ... existing settings ...
    
    # Add your payment method settings
    mycustom_api_key: str = Field(default="")
    mycustom_endpoint: str = Field(default="https://api.example.com")
```

## Payment Method Priority

Payment methods are checked in the order they were registered. The first method where `can_handle()` returns `True` will be used.

Default order:
1. Cashu
2. Lightning
3. USDT
4. On-chain Bitcoin
5. Custom methods (in registration order)

## Database Considerations

### Current ApiKey Fields

The `ApiKey` model currently includes:
- `balance`: Available balance in msats
- `reserved_balance`: Reserved for pending requests
- `refund_address`: Address/LNURL for refunds
- `refund_currency`: Currency for refunds (sat, msat, etc.)
- `refund_mint_url`: Cashu mint URL for refunds
- `key_expiry_time`: Expiry timestamp for auto-refund
- `total_spent`: Total msats spent
- `total_requests`: Total API requests made

### Recommended Extensions

For multi-payment-method support, consider adding:
```python
# In core/db.py ApiKey model
payment_method_type: str | None = Field(
    default=None,
    description="Payment method used (cashu, lightning, usdt, bitcoin)"
)
payment_metadata: str | None = Field(
    default=None,
    description="JSON-encoded payment method specific metadata"
)
```

This allows:
- Automatic payment method detection for refunds
- Storage of method-specific data without schema changes
- Better analytics and reporting

## Testing

### Unit Tests

Test each payment method in isolation:

```python
from routstr.payment.methods import CashuPaymentMethod

async def test_cashu_can_handle():
    method = CashuPaymentMethod()
    assert method.can_handle("cashuA...")
    assert not method.can_handle("lnbc...")

async def test_cashu_parse_credential():
    method = CashuPaymentMethod()
    creds = await method.parse_credential("cashuA...")
    assert creds.payment_type == "cashu"
    assert creds.metadata is not None
```

### Integration Tests

Test end-to-end payment flows:

```python
async def test_payment_flow():
    # Create credential
    credential = generate_test_credential()
    
    # Validate and credit
    key = await validate_bearer_key(credential, session)
    assert key.balance > 0
    
    # Topup
    await topup_wallet_endpoint(credential, key, session)
    
    # Refund
    result = await refund_wallet_endpoint(key)
    assert "token" in result or "recipient" in result
```

## Security Considerations

1. **Credential Validation**: Always validate credentials thoroughly before processing
2. **Idempotency**: Prevent duplicate processing of the same payment
3. **Atomic Operations**: Use atomic SQL updates to prevent race conditions
4. **Private Keys**: Never log or expose private keys
5. **Rate Limiting**: Implement rate limits on payment processing endpoints
6. **Confirmation Requirements**: Wait for sufficient blockchain confirmations
7. **Amount Limits**: Consider minimum and maximum payment amounts
8. **Refund Address Validation**: Verify refund addresses belong to the original payer

## API Endpoints

### Create Wallet

```http
GET /create?initial_balance_token=<credential>
```

Creates a new API key from a payment credential.

### Topup

```http
POST /topup
Content-Type: application/json

{
  "cashu_token": "<credential>"
}
```

Adds funds to an existing API key.

### Refund

```http
POST /refund
Authorization: Bearer <credential>
```

Refunds the remaining balance.

### Balance Info

```http
GET /info
Authorization: Bearer sk-<key>
```

Returns current balance and reserved amount.

## Future Enhancements

1. **Multi-Chain Support**: Support USDT on multiple chains simultaneously
2. **Automatic Conversion**: Convert between payment methods automatically
3. **Partial Refunds**: Allow refunding specific amounts instead of full balance
4. **Payment History**: Track all incoming/outgoing payments per key
5. **Scheduled Refunds**: Auto-refund after expiry time
6. **Payment Webhooks**: Notify external systems of payment events
7. **Fee Customization**: Per-method fee configuration
8. **Payment Routing**: Intelligent routing based on amount, speed, fees

## Resources

- [Cashu Protocol](https://cashu.space/)
- [Lightning Network](https://lightning.network/)
- [USDT Documentation](https://tether.to/)
- [Bitcoin Core RPC](https://bitcoincore.org/en/doc/)
- [Web3.py Documentation](https://web3py.readthedocs.io/)

## Support

For questions or issues with payment methods:
1. Check this documentation
2. Review the source code in `routstr/payment/methods.py`
3. Open an issue on GitHub
4. Join our community chat
