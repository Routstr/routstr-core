# Authentication

Routstr uses API key authentication for all protected endpoints. This guide covers how to create, use, and manage API keys.

## API Key Creation

### From eCash Token

Create an API key by depositing an eCash token:

**Note: The POST /v1/wallet/create endpoint is coming soon. Currently, you can use Cashu tokens directly as API credentials in the Authorization header. The token is hashed on the server, and the hash acts as an API key with the token's balance.**

```bash
POST /v1/wallet/create
Content-Type: application/json

{
  "cashu_token": "cashuAeyJ0b2tlbiI6W3sibWludCI6Imh0dHBzOi8vbWlu..."
}
```

**Request Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cashu_token` | string | Yes | Base64-encoded Cashu token |

**Response:**

```json
{
  "api_key": "sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p",
  "balance": 10000,
  "created_at": "2024-01-01T00:00:00Z",
  "key_id": "key_123456"
}
```

### From Lightning Invoice (Coming Soon)

```bash
POST /v1/wallet/create/lightning
Content-Type: application/json

{
  "amount_sats": 10000,
  "name": "Lightning Key"
}
```

Response includes Lightning invoice for payment.

## Using API Keys

### Header Authentication

Include the API key in the Authorization header:

```bash
curl https://your-node.com/v1/chat/completions \
  -H "Authorization: Bearer sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"Hello"}]}'
```

### Query Parameter (Not Recommended)

For tools that don't support headers:

```bash
GET /v1/models?api_key=sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p
```

⚠️ **Warning**: Query parameters may be logged. Use headers when possible.

## Key Management

### Check Balance

Get current balance and usage statistics:

```bash
GET /v1/wallet/balance
Authorization: Bearer sk-...

Response:
{
  "balance": 8546,
  "total_deposited": 10000,
  "total_spent": 1454,
  "last_used": "2024-01-01T12:34:56Z",
  "created_at": "2024-01-01T00:00:00Z",
  "expires_at": null,
  "key_info": {
    "name": "Production Key",
    "key_id": "key_123456"
  }
}
```

### Top Up Balance

Add funds to existing key:

```bash
POST /v1/wallet/topup
Authorization: Bearer sk-...
Content-Type: application/json

{
  "cashu_token": "cashuAeyJ0b2tlbiI6W3..."
}

Response:
{
  "old_balance": 8546,
  "added_amount": 5000,
  "new_balance": 13546,
  "transaction_id": "txn_789"
}
```

### List Transactions

View transaction history:

```bash
GET /v1/wallet/transactions?limit=10
Authorization: Bearer sk-...

Response:
{
  "transactions": [
    {
      "id": "txn_123",
      "type": "usage",
      "amount": -154,
      "balance_after": 8546,
      "description": "gpt-3.5-turbo: 50 prompt + 150 completion tokens",
      "timestamp": "2024-01-01T12:34:56Z"
    },
    {
      "id": "txn_122",
      "type": "deposit",
      "amount": 10000,
      "balance_after": 10000,
      "description": "Initial deposit",
      "timestamp": "2024-01-01T00:00:00Z"
    }
  ],
  "has_more": false,
  "total": 2
}
```

## Security Best Practices

### API Key Storage

**Do:**

- Store keys in environment variables
- Use secret management systems
- Encrypt keys at rest
- Implement key rotation

**Don't:**

- Commit keys to version control
- Share keys between environments
- Log keys in plain text
- Expose keys in client-side code

### Environment Variables

```bash
# .env file
ROUTSTR_API_KEY=sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p
ROUTSTR_BASE_URL=https://your-node.com/v1

# Usage in code
import os
api_key = os.getenv("ROUTSTR_API_KEY")
```

### Key Rotation

Regularly rotate API keys:

```python
# 1. Create new key
new_key = create_api_key(balance=old_key_balance)

# 2. Update applications
update_environment_variable("ROUTSTR_API_KEY", new_key)

# 3. Test new key
test_api_connection(new_key)

# 4. Withdraw old key balance
withdraw_balance(old_key)
```

## Authentication Errors

### Invalid API Key

```json
{
  "error": {
    "type": "authentication_failed",
    "message": "Invalid API key",
    "code": "invalid_api_key"
  }
}
```

**Status Code:** 401

**Common Causes:**

- Typo in API key
- Key doesn't exist
- Key has been deleted

### Expired API Key

```json
{
  "error": {
    "type": "authentication_failed",
    "message": "API key has expired",
    "code": "key_expired",
    "details": {
      "expired_at": "2024-01-01T00:00:00Z"
    }
  }
}
```

**Status Code:** 401

**Resolution:**

- Create a new API key
- Contact admin if refund address was set

### Insufficient Balance

```json
{
  "error": {
    "type": "insufficient_balance",
    "message": "Insufficient balance for request",
    "code": "payment_required",
    "details": {
      "balance": 100,
      "required": 154,
      "shortfall": 54
    }
  }
}
```

**Status Code:** 402

**Resolution:**

- Top up the API key balance
- Use a more economical model
- Optimize request parameters

## Advanced Authentication

### Per-Request Tokens (Coming Soon)

Pay per request without maintaining a balance:

```bash
curl https://your-node.com/v1/chat/completions \
  -H "X-Cashu: cashuAeyJ0b2tlbiI6W3..." \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-3.5-turbo","messages":[...]}'
```

Response includes change:

```
X-Cashu: cashuAeyJjaGFuZ2UiOlt7...
```

### Multi-Key Authentication

Use multiple keys for different purposes:

```python
# Production key for main app
PROD_KEY = os.getenv("ROUTSTR_PROD_KEY")

# Development key for testing
DEV_KEY = os.getenv("ROUTSTR_DEV_KEY")

# Analytics key with restricted permissions
ANALYTICS_KEY = os.getenv("ROUTSTR_ANALYTICS_KEY")

# Choose key based on environment
api_key = PROD_KEY if is_production() else DEV_KEY
```

### Delegated Authentication

Create sub-keys with limited permissions:

```bash
POST /v1/wallet/create/subkey
Authorization: Bearer sk-parent-key
Content-Type: application/json

{
  "name": "Limited Subkey",
  "balance_limit": 1000,
  "allowed_models": ["gpt-3.5-turbo"],
  "expires_in_hours": 24
}
```

## Rate Limiting

Rate limits are applied per API key:

### Default Limits

| Metric | Limit | Window |
|--------|-------|--------|
| Requests | 1000 | 1 minute |
| Tokens | 1,000,000 | 1 hour |
| Concurrent | 10 | - |

### Rate Limit Headers

```
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 999
X-RateLimit-Reset: 1640995200
X-RateLimit-Type: requests_per_minute
```

### Handling Rate Limits

```python
import time
from typing import Optional

def make_request_with_retry(
    client,
    max_retries: int = 3
) -> Optional[Response]:
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(...)
            return response
        except RateLimitError as e:
            if attempt < max_retries - 1:
                # Extract retry-after from error
                retry_after = e.retry_after or 60
                print(f"Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
            else:
                raise
```

## IP Whitelisting

Restrict API key usage by IP:

```bash
POST /v1/wallet/update
Authorization: Bearer sk-...
Content-Type: application/json

{
  "allowed_ips": [
    "192.168.1.100",
    "10.0.0.0/24"
  ]
}
```

## Monitoring

### Usage Alerts

Set up usage notifications:

```bash
POST /v1/wallet/alerts
Authorization: Bearer sk-...
Content-Type: application/json

{
  "low_balance_threshold": 1000,
  "daily_spend_limit": 5000,
  "webhook_url": "https://your-app.com/webhook"
}
```

### Audit Logging

All API key usage is logged:

```json
{
  "timestamp": "2024-01-01T12:34:56Z",
  "api_key_id": "key_123456",
  "endpoint": "/v1/chat/completions",
  "method": "POST",
  "ip_address": "192.168.1.100",
  "user_agent": "OpenAI-Python/1.0",
  "cost_sats": 154,
  "response_status": 200
}
```

## Next Steps

- [Endpoints](endpoints.md) - Complete endpoint reference
- [Errors](errors.md) - Error handling guide
- [Using the API](../user-guide/using-api.md) - Integration examples
