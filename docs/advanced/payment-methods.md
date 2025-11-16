# Payment Methods Architecture

This document describes the modular payment method system for temporary balance management in Routstr.

## Overview

The payment method system allows Routstr to accept multiple types of payments for temporary balance top-ups, including:

- **Cashu eCash tokens** (fully implemented)
- **Bitcoin Lightning Network** (pseudo-implemented)
- **USDT/Tether** (pseudo-implemented)
- **On-chain Bitcoin** (pseudo-implemented)

## Architecture

### Abstract Base Class: `PaymentMethod`

All payment methods inherit from the abstract `PaymentMethod` class, which defines the required interface:

```python
class PaymentMethod(ABC):
    @property
    @abstractmethod
    def method_name(self) -> str:
        """Unique identifier for this payment method"""
        pass
    
    @abstractmethod
    async def receive(self, token: str) -> PaymentTokenInfo:
        """Receive and process a payment token"""
        pass
    
    @abstractmethod
    async def send(
        self, amount: int, unit: str, destination: str | None = None
    ) -> tuple[int, str]:
        """Send payment and generate a token/receipt"""
        pass
    
    @abstractmethod
    async def validate_token(self, token: str) -> bool:
        """Validate token format without processing it"""
        pass
    
    @abstractmethod
    async def get_balance(self, unit: str) -> int:
        """Get current balance held by this payment method"""
        pass
```

### Payment Method Factory

The `PaymentMethodFactory` class provides:

1. **Method instantiation**: `get_method(method_name)`
2. **Auto-detection**: `detect_method(token)` - automatically detects payment method from token format
3. **Discovery**: `get_available_methods()` and `get_implemented_methods()`

## Current Implementations

### 1. Cashu Payment Method (Fully Implemented)

**Status**: ✅ Production Ready

The Cashu payment method is the current production implementation, supporting:

- Token reception and validation
- DLEQ proof verification
- Multi-mint support with automatic swapping
- Lightning invoice settlement
- Balance tracking across multiple mints

**Token Format**: `cashuAeyJ0b2tlbiI6W3sibWludCI6Imh0dHBzOi8v...`

**Example Usage**:
```python
from routstr.payment.methods import PaymentMethodFactory

# Get Cashu payment method
cashu = PaymentMethodFactory.get_method("cashu")

# Receive a token
payment_info = await cashu.receive(cashu_token)
print(f"Received {payment_info['amount']} {payment_info['unit']}")

# Send a token
amount, token = await cashu.send(10000, "sat", mint_url="https://mint.example.com")
```

### 2. Lightning Payment Method (Pseudo-Implemented)

**Status**: 🚧 Not Yet Implemented

The Lightning payment method will support direct Lightning Network payments without Cashu intermediary.

**Required for Full Implementation**:

1. **Lightning Node Integration**
   - Choose node software: LND, CLN (Core Lightning), or LNbits
   - Add appropriate library:
     - LND: `lnd-grpc` or `lndgrpc`
     - CLN: `pyln-client` or `cln-grpc`
     - LNbits: Use REST API with `httpx`

2. **Configuration Settings**
   ```python
   # In settings.py
   LIGHTNING_NODE_TYPE = "lnd"  # or "cln" or "lnbits"
   
   # For LND
   LND_MACAROON_PATH = "/path/to/admin.macaroon"
   LND_TLS_CERT_PATH = "/path/to/tls.cert"
   LND_GRPC_ENDPOINT = "localhost:10009"
   
   # For CLN
   CLN_RUNE = "rune_string_here"
   CLN_GRPC_ENDPOINT = "localhost:9735"
   
   # For LNbits
   LNBITS_ADMIN_KEY = "admin_key_here"
   LNBITS_API_ENDPOINT = "https://lnbits.example.com"
   ```

3. **Invoice Management**
   - Generate invoices for incoming payments
   - Monitor invoice status (pending/settled/expired)
   - Store payment hashes to prevent double-crediting

4. **Payment Routing**
   - Implement payment sending with timeout handling
   - Handle routing failures and retries
   - Support multi-path payments (MPP)

5. **LNURL Support**
   - Parse LNURL-pay requests
   - Fetch invoices from LNURL services
   - Support Lightning addresses (user@domain.com)

**Token Formats**:
- BOLT11 invoice: `lnbc10u1p3...`
- Lightning address: `user@domain.com`
- LNURL: `lnurl1dp68gurn8ghj7...`

**Example Implementation Snippet**:
```python
# Install: pip install lndgrpc
from lndgrpc import LNDClient

class LightningPaymentMethod(PaymentMethod):
    def __init__(self):
        self.lnd = LNDClient(
            macaroon_filepath=settings.lnd_macaroon_path,
            cert_filepath=settings.lnd_tls_cert_path,
            grpc_endpoint=settings.lnd_grpc_endpoint,
        )
    
    async def receive(self, token: str) -> PaymentTokenInfo:
        # Decode BOLT11 invoice
        decoded = self.lnd.decode_pay_req(token)
        payment_hash = decoded.payment_hash
        
        # Check if payment is settled
        invoice = self.lnd.lookup_invoice(payment_hash)
        if invoice.state != "SETTLED":
            raise ValueError("Payment not settled")
        
        # Check not already credited
        if await is_payment_hash_used(payment_hash):
            raise ValueError("Payment already credited")
        
        return PaymentTokenInfo(
            amount=decoded.num_satoshis * 1000,  # Convert to msats
            unit="msat",
            mint_url=self.lnd.get_info().identity_pubkey,
        )
```

### 3. USDT Payment Method (Pseudo-Implemented)

**Status**: 🚧 Not Yet Implemented

The USDT payment method will support Tether stablecoin payments on various blockchains.

**Required for Full Implementation**:

1. **Blockchain Integration**
   - Choose blockchain(s): Ethereum (ERC-20), Tron (TRC-20), Polygon
   - Add web3 library: `pip install web3` (Ethereum) or `tronpy` (Tron)
   - Connect to node or service (Infura, Alchemy, QuickNode)

2. **Smart Contract Configuration**
   ```python
   # USDT Contract Addresses
   USDT_ETHEREUM = "0xdac17f958d2ee523a2206206994597c13d831ec7"
   USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
   USDT_POLYGON = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
   
   # Blockchain RPC endpoints
   ETHEREUM_RPC = "https://mainnet.infura.io/v3/YOUR_PROJECT_ID"
   TRON_RPC = "https://api.trongrid.io"
   ```

3. **Wallet Management**
   - Generate HD wallet for deposit addresses
   - Secure private key storage (consider HSM or KMS)
   - Address monitoring for incoming transactions

4. **Transaction Processing**
   - Monitor mempool for incoming transactions
   - Wait for sufficient confirmations (e.g., 12 for Ethereum)
   - Parse transaction logs for USDT transfer events
   - Prevent double-crediting

5. **Price Oracle**
   - Integrate price feed (Chainlink, CoinGecko, Binance API)
   - Convert USDT amounts to BTC/sats
   - Handle price volatility

6. **Gas Fee Management**
   - Estimate gas fees for outgoing transactions
   - Maintain ETH/TRX balance for gas
   - Implement fee bumping for stuck transactions

**Token Format**: Transaction hash (e.g., `0x1234...` for Ethereum, 64-char hex for Tron)

**Example Implementation Snippet**:
```python
# Install: pip install web3
from web3 import Web3

class USDTPaymentMethod(PaymentMethod):
    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(settings.ethereum_rpc))
        self.usdt_contract = self.w3.eth.contract(
            address=settings.usdt_ethereum,
            abi=USDT_ABI,
        )
    
    async def receive(self, token: str) -> PaymentTokenInfo:
        # Get transaction receipt
        tx_receipt = self.w3.eth.get_transaction_receipt(token)
        
        # Check confirmations
        current_block = self.w3.eth.block_number
        confirmations = current_block - tx_receipt.blockNumber
        if confirmations < 12:
            raise ValueError(f"Insufficient confirmations: {confirmations}/12")
        
        # Parse USDT transfer event
        transfer_events = self.usdt_contract.events.Transfer().process_receipt(tx_receipt)
        
        for event in transfer_events:
            if event.args.to == settings.usdt_deposit_address:
                usdt_amount = event.args.value / 1e6  # USDT has 6 decimals
                
                # Convert to BTC using oracle
                rate = await get_usdt_to_btc_rate()
                btc_amount = usdt_amount * rate
                sats = int(btc_amount * 1e8)
                
                return PaymentTokenInfo(
                    amount=sats * 1000,  # Convert to msats
                    unit="msat",
                    mint_url=f"ethereum:{tx_receipt.blockNumber}",
                )
        
        raise ValueError("No USDT transfer found to our address")
```

### 4. On-Chain Bitcoin Payment Method (Pseudo-Implemented)

**Status**: 🚧 Not Yet Implemented

The on-chain Bitcoin payment method will support direct blockchain payments.

**Required for Full Implementation**:

1. **Bitcoin Node Connection**
   - Run Bitcoin Core node or use service (Blockstream, BlockCypher)
   - Add library: `pip install python-bitcoinlib` or use RPC directly

2. **Configuration**
   ```python
   BITCOIN_RPC_USER = "rpcuser"
   BITCOIN_RPC_PASSWORD = "rpcpassword"
   BITCOIN_RPC_HOST = "localhost"
   BITCOIN_RPC_PORT = 8332
   REQUIRED_CONFIRMATIONS = 6
   ```

3. **Address Management**
   - Implement HD wallet (BIP32/44)
   - Generate unique deposit addresses per user
   - Monitor addresses for incoming transactions

4. **Transaction Processing**
   - Wait for required confirmations (typically 6)
   - Handle RBF (Replace-By-Fee) transactions
   - Detect double-spend attempts
   - Parse transaction outputs

5. **UTXO Management**
   - Track unspent outputs
   - Select UTXOs for outgoing payments
   - Implement coin selection algorithm
   - Handle change outputs

6. **Fee Estimation**
   - Query mempool for current fee rates
   - Implement dynamic fee estimation
   - Support fee bumping (RBF)
   - Consider batching multiple outputs

**Token Format**: Transaction ID (64-character hex string)

## Usage Examples

### Auto-Detection

The system can automatically detect the payment method from token format:

```python
from routstr.payment.methods import PaymentMethodFactory

# Auto-detect from token
token = "cashuAeyJ0b2tlbiI6..."
method = PaymentMethodFactory.detect_method(token)
print(f"Detected method: {method.method_name}")  # "cashu"

# Process payment
payment_info = await method.receive(token)
```

### Explicit Method Selection

You can also explicitly specify the payment method:

```python
# Use specific payment method
lightning = PaymentMethodFactory.get_method("lightning")
payment_info = await lightning.receive("lnbc10u1p3...")
```

### API Endpoint Integration

The `/v1/balance/topup` endpoint now supports multiple payment methods:

```bash
# Auto-detect from token (backward compatible)
curl -X POST http://localhost:8000/v1/balance/topup \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"cashu_token": "cashuAeyJ0..."}'

# Explicitly specify payment method
curl -X POST http://localhost:8000/v1/balance/topup \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "cashu_token": "lnbc10u1p3...",
    "payment_method": "lightning"
  }'
```

## Adding a New Payment Method

To add a new payment method:

1. **Create Payment Method Class**
   
   Create a new class inheriting from `PaymentMethod`:
   
   ```python
   class MyPaymentMethod(PaymentMethod):
       @property
       def method_name(self) -> str:
           return "my_method"
       
       async def receive(self, token: str) -> PaymentTokenInfo:
           # Implement token reception logic
           pass
       
       async def send(self, amount: int, unit: str, destination: str | None = None) -> tuple[int, str]:
           # Implement payment sending logic
           pass
       
       async def validate_token(self, token: str) -> bool:
           # Implement token validation
           pass
       
       async def get_balance(self, unit: str) -> int:
           # Implement balance query
           pass
   ```

2. **Register in Factory**
   
   Add the method to `PaymentMethodFactory`:
   
   ```python
   @classmethod
   def get_method(cls, method_name: str) -> PaymentMethod:
       if method_name not in cls._instances:
           # ... existing methods ...
           elif method_name == "my_method":
               cls._instances[method_name] = MyPaymentMethod()
           # ...
   ```

3. **Add Auto-Detection Logic**
   
   Update `detect_method()` to recognize your token format:
   
   ```python
   @classmethod
   def detect_method(cls, token: str) -> PaymentMethod:
       # ... existing detection ...
       if token.startswith("myprefix"):
           return cls.get_method("my_method")
       # ...
   ```

4. **Add Tests**
   
   Create integration tests for your payment method:
   
   ```python
   async def test_my_payment_method():
       method = PaymentMethodFactory.get_method("my_method")
       
       # Test token validation
       assert await method.validate_token("myprefix123...")
       
       # Test receiving
       payment_info = await method.receive("myprefix123...")
       assert payment_info["amount"] > 0
       
       # Test sending
       amount, receipt = await method.send(1000, "sat", "dest")
       assert receipt is not None
   ```

## Configuration

Add payment method settings to your environment:

```bash
# Cashu (current implementation)
CASHU_MINTS=https://mint1.com,https://mint2.com

# Lightning (when implemented)
LIGHTNING_NODE_TYPE=lnd
LND_MACAROON_PATH=/path/to/admin.macaroon
LND_TLS_CERT_PATH=/path/to/tls.cert
LND_GRPC_ENDPOINT=localhost:10009

# USDT (when implemented)
USDT_BLOCKCHAIN=ethereum
ETHEREUM_RPC=https://mainnet.infura.io/v3/YOUR_KEY
USDT_DEPOSIT_ADDRESS=0x...
USDT_PRIVATE_KEY=encrypted_or_in_hsm

# Bitcoin (when implemented)
BITCOIN_RPC_HOST=localhost
BITCOIN_RPC_PORT=8332
BITCOIN_RPC_USER=rpcuser
BITCOIN_RPC_PASSWORD=rpcpassword
```

## Security Considerations

### Private Key Management

- **Never** store private keys in plain text
- Use Hardware Security Modules (HSM) for production
- Consider AWS KMS, Azure Key Vault, or Google Cloud KMS
- Implement key rotation policies

### Transaction Validation

- Always wait for sufficient confirmations
- Validate transaction recipients match your addresses
- Check for double-spend attempts
- Store transaction IDs to prevent double-crediting

### Rate Limiting

- Implement rate limits on payment endpoints
- Use exponential backoff for failed attempts
- Monitor for suspicious patterns

### Monitoring

- Set up alerts for:
  - Unusual transaction amounts
  - Failed payment attempts
  - Balance discrepancies
  - Unconfirmed transactions older than expected

## Future Enhancements

Potential future payment methods to consider:

1. **Liquid Network** - Bitcoin sidechain with fast settlements
2. **Monero (XMR)** - Privacy-focused cryptocurrency
3. **Fiat On-Ramps** - Credit card integration via Stripe/PayPal
4. **Other Stablecoins** - USDC, DAI, BUSD
5. **Alternative L2s** - Rootstock (RSK), Stacks

## Troubleshooting

### Payment Method Not Detected

If auto-detection fails, explicitly specify the method:

```python
method = PaymentMethodFactory.get_method("cashu")
```

### NotImplementedError

This means the payment method is not fully implemented yet. Check the implementation status above.

### Token Validation Fails

Ensure the token format is correct for the payment method:
- Cashu: Must start with "cashuA" or "cashuB"
- Lightning: Must start with "ln" or contain "@"
- USDT: Must be a valid transaction hash
- Bitcoin: Must be a 64-character hex string

## Contributing

When implementing a new payment method, please:

1. Follow the abstract interface exactly
2. Add comprehensive error handling
3. Write integration tests
4. Update this documentation
5. Consider security implications
6. Add monitoring and logging

For questions or contributions, see [CONTRIBUTING.md](../../CONTRIBUTING.md).
