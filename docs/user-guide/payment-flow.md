# Payment Flow

Understanding how payments work in Routstr is key to using the system effectively. This guide explains the payment process in detail.

## Overview

Routstr uses a pre-funded account model where:

1. Users deposit eCash tokens to create an API key
2. Each API request deducts from the balance
3. Users can withdraw remaining balance as eCash

## Creating an API Key

### Step 1: Obtain eCash Token

Get a Cashu token from any compatible source:

**Option A: From a Cashu Wallet**

```bash
# Example: Creating a 10,000 sat token
cashu send 10000
```

**Option B: Lightning Invoice**

```bash
# Some mints support direct Lightning deposits
curl -X POST https://mint.example.com/v1/mint/quote/bolt11 \
  -d '{"amount": 10000, "unit": "sat"}'
```

**Option C: Test Tokens**

```bash
# Get test tokens from testnet mints
# Check mint documentation for faucets
```

### Step 2: Create API Key

**Note: The POST /v1/wallet/create endpoint is coming soon. Currently, you can use Cashu tokens directly as API keys in the Authorization header.**

Send your token to Routstr:

```bash
curl -X POST https://api.routstr.com/v1/wallet/create \
  -H "Content-Type: application/json" \
  -d '{
    "cashu_token": "cashuAeyJ0b2tlbiI6W3sibWludCI6Imh0dHBzOi8vbWlu..."
  }'
```

**Request Parameters:**

- `cashu_token` (required): The eCash token to deposit

**Response:**

```json
{
  "api_key": "sk-1234567890abcdef",
  "balance": 10000000,
  "created_at": "2024-01-01T00:00:00Z"
}
```

### Step 3: Verify Balance

Check your key's balance:

```bash
curl -X GET https://api.routstr.com/v1/wallet/balance \
  -H "Authorization: Bearer sk-1234567890abcdef"
```

Response:

```json
{
  "balance": 10000000,
  "total_deposited": 10000000,
  "total_spent": 0,
  "last_used": null
}
```

## Making API Requests

### Cost Calculation

Costs are calculated based on:

1. **Request Type**
   - Chat completions
   - Embeddings
   - Image generation
   - Audio processing

2. **Token Usage**
   - Input tokens (prompt)
   - Output tokens (response)
   - Model-specific rates

3. **Additional Costs**
   - Base request fee
   - Image generation fees
   - Audio processing time

### Example: Chat Completion

```python
import openai

client = openai.OpenAI(
    api_key="sk-1234567890abcdef",
    base_url="https://api.routstr.com/v1"
)

# Make request
response = client.chat.completions.create(
    model="gpt-3.5-turbo",
    messages=[
        {"role": "user", "content": "Hello, how are you?"}
    ]
)

# Check usage
print(f"Input tokens: {response.usage.prompt_tokens}")
print(f"Output tokens: {response.usage.completion_tokens}")
print(f"Total tokens: {response.usage.total_tokens}")
```

### Cost Breakdown

For the above request:

```
Model: gpt-3.5-turbo
Input tokens: 13
Output tokens: 27
Model rates: $0.0015/1K input, $0.002/1K output

USD Cost = (13/1000 * 0.0015) + (27/1000 * 0.002) = $0.0000735
BTC/USD Rate: $50,000
BTC Cost = 0.0000735 / 50000 = 0.00000000147 BTC = 147 sats
With fees (5%): 154 sats

Final cost: 154 sats
```

## Balance Management

### Monitoring Usage

Track your usage in real-time:

```bash
# Get current balance
curl -X GET https://api.routstr.com/v1/wallet/balance \
  -H "Authorization: Bearer your-api-key"

# View recent transactions (through admin dashboard)
# Access at https://api.routstr.com/admin/
```

### Low Balance Handling

When balance is insufficient:

```json
{
  "error": {
    "type": "insufficient_balance",
    "message": "Insufficient balance. Current: 100 sats, Required: 154 sats",
    "code": "payment_required"
  }
}
```

### Topping Up

Add funds to existing key:

```bash
curl -X POST https://api.routstr.com/v1/wallet/topup \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "cashu_token": "cashuAeyJ0b2..."
  }'
```

## Withdrawing Balance

### Via Admin Dashboard

1. Navigate to `/admin/`
2. Enter admin password
3. Find your API key
4. Click "Withdraw"
5. Receive eCash token

### Via API (if enabled)

```bash
curl -X POST https://api.routstr.com/v1/wallet/withdraw \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "amount": 5000,
    "mint": "https://mint.minibits.cash/Bitcoin"
  }'
```

## Payment Security

### Token Validation

Routstr validates tokens by:

1. Checking signature validity
2. Verifying with the issuing mint
3. Ensuring no double-spending
4. Confirming sufficient value

### Failed Payments

Common failure reasons:

- Invalid token signature
- Already spent token
- Untrusted mint
- Network issues with mint

### Refund Policy

- Unused balance can be withdrawn anytime
- Expired keys with balance can be refunded
- Node operators may have additional policies

## Advanced Features

### Multi-Mint Support

Routstr accepts tokens from multiple mints:

```bash
CASHU_MINTS=https://mint1.com,https://mint2.com,https://mint3.com
```

Benefits:

- Redundancy if one mint is down
- User choice of mints
- Geographic distribution

### Automatic Payouts

Configure automatic Lightning payouts:

```bash
RECEIVE_LN_ADDRESS=satoshi@getalby.com
```

When enabled:

- Balances above threshold are swept
- Converted to Lightning payments
- Sent to configured address

### Per-Request Payments (Coming Soon)

Future support for Nut-24 headers:

```bash
curl -X POST https://api.routstr.com/v1/chat/completions \
  -H "x-cashu: cashuAeyJ0..." \
  -H "Content-Type: application/json" \
  -d '{...}'
```

Response includes change:

```
HTTP/1.1 200 OK
x-cashu: cashuAeyJjaGFuZ2Ui...
```

## Best Practices

### API Key Management

1. **Separate Keys per Application**
   - Easier tracking
   - Better security
   - Independent budgets

2. **Set Expiration Dates**
   - Automatic cleanup
   - Security improvement
   - Budget control

3. **Monitor Balances**
   - Set up alerts
   - Regular checks
   - Usage analytics

### Cost Optimization

1. **Choose Appropriate Models**
   - Smaller models for simple tasks
   - Larger models only when needed

2. **Optimize Prompts**
   - Concise, clear instructions
   - Avoid unnecessary tokens

3. **Use Streaming**
   - Early termination possible
   - Better user experience

### Security

1. **Secure Storage**
   - Environment variables
   - Secrets management
   - Never in code

2. **Network Security**
   - Always use HTTPS
   - Verify certificates
   - Consider Tor for privacy

3. **Regular Rotation**
   - Change keys periodically
   - Withdraw unused funds
   - Audit usage logs

## Troubleshooting

### Payment Rejected

**Error:** "Invalid token"

- Check token format
- Verify mint is trusted
- Ensure not already spent

**Error:** "Insufficient value"

- Token value too low
- Check current pricing
- Add larger token

### Balance Discrepancies

- Allow for price fluctuations
- Check model pricing updates
- Review transaction history

### Mint Issues

- Try different mint from list
- Check mint status
- Contact mint operator

## Next Steps

- [Using the API](using-api.md) - Integration guide
- [Admin Dashboard](admin-dashboard.md) - Account management
- [Models & Pricing](models-pricing.md) - Cost details
