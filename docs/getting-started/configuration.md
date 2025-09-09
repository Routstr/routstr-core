# Configuration

Routstr Core is configured via a single settings row in the database. Environment variables are only used on first run to seed that row (with a few computed defaults like `ONION_URL`). After that, the database is the source of truth. You can update settings at runtime via the admin API. `DATABASE_URL` is always env-only.

## Environment Variables

### Core Settings

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `UPSTREAM_BASE_URL` | Base URL of the OpenAI-compatible API to proxy | - | ✅ |
| `UPSTREAM_API_KEY` | API key for the upstream service | - | ❌ |
| `ADMIN_PASSWORD` | Password for admin dashboard access | - | ⚠️ |

### Node Information

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `NAME` | Public name of your Routstr node | `ARoutstrNode` | ❌ |
| `DESCRIPTION` | Description of your node | `A Routstr Node` | ❌ |
| `NPUB` | Nostr public key for node identity | - | ❌ |
| `HTTP_URL` | Public HTTP URL of your node | - | ❌ |
| `ONION_URL` | Tor hidden service URL | - | ❌ |

### Cashu Configuration

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `CASHU_MINTS` | Comma-separated list of trusted Cashu mint URLs | `https://mint.minibits.cash/Bitcoin` | ❌ |
| `RECEIVE_LN_ADDRESS` | Lightning address for automatic payouts | - | ❌ |

### Pricing Configuration

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `FIXED_PRICING` | Force fixed per-request pricing (ignore model token pricing) | `false` | ❌ |
| `FIXED_COST_PER_REQUEST` | Fixed cost per API request in sats | `1` | ❌ |
| `FIXED_PER_1K_INPUT_TOKENS` | Optional override: sats per 1000 input tokens | `0` | ❌ |
| `FIXED_PER_1K_OUTPUT_TOKENS` | Optional override: sats per 1000 output tokens | `0` | ❌ |
| `EXCHANGE_FEE` | Exchange rate markup (1.005 = 0.5% fee) | `1.005` | ❌ |
| `UPSTREAM_PROVIDER_FEE` | Provider fee markup (1.05 = 5% fee) | `1.05` | ❌ |

### Network & Discovery

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `CORS_ORIGINS` | Comma-separated list of allowed CORS origins | `*` | ❌ |
| `TOR_PROXY_URL` | SOCKS5 proxy URL for Tor connections | `socks5://127.0.0.1:9050` | ❌ |
| `RELAYS` | Comma-separated nostr relays used for provider discovery | sane defaults | ❌ |
| `PROVIDERS_REFRESH_INTERVAL_SECONDS` | Provider cache refresh interval | `300` | ❌ |

### Logging Configuration

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) | `INFO` | ❌ |
| `ENABLE_CONSOLE_LOGGING` | Enable console log output | `true` | ❌ |

### Other

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `CHAT_COMPLETIONS_API_VERSION` | Append `api-version` to `/chat/completions` (Azure OpenAI) | - | ❌ |
| `DATABASE_URL` | SQLite database connection string | `sqlite+aiosqlite:///keys.db` | ❌ |
| `REFUND_CACHE_TTL_SECONDS` | Cache TTL for refund responses (seconds) | `3600` | ❌ |

## Configuration Examples

### Basic OpenAI Proxy

```bash
# .env
UPSTREAM_BASE_URL=https://api.openai.com/v1
UPSTREAM_API_KEY=sk-...
ADMIN_PASSWORD=my-secure-password
```

### Custom AI Provider

```bash
# .env
UPSTREAM_BASE_URL=https://api.anthropic.com/v1
UPSTREAM_API_KEY=your-anthropic-key
MODELS_PATH=/app/config/anthropic-models.json
```

### Azure OpenAI (optional)

```bash
# .env
UPSTREAM_BASE_URL=https://<resource>.openai.azure.com/openai/deployments/<deployment>
UPSTREAM_API_KEY=<azure_api_key>
CHAT_COMPLETIONS_API_VERSION=2024-05-01-preview
```

### High-Security Setup

```bash
# .env
UPSTREAM_BASE_URL=https://api.openai.com/v1
UPSTREAM_API_KEY=sk-...
ADMIN_PASSWORD=very-long-secure-password-here
CORS_ORIGINS=https://myapp.com,https://app.myapp.com
TOR_PROXY_URL=socks5://tor:9050
LOG_LEVEL=WARNING
```

### Public Node Configuration

```bash
# .env
NAME=Lightning AI Gateway
DESCRIPTION=Fast and reliable AI API access with Bitcoin payments
NPUB=npub1abcd...
HTTP_URL=https://api.lightning-ai.com
ONION_URL=http://lightningai.onion
CASHU_MINTS=https://mint1.com,https://mint2.com
```

## Pricing

- Default: pricing comes from your `models.json`.
- Force fixed per-request pricing: set `FIXED_PRICING=true` and `FIXED_COST_PER_REQUEST`.
- Optional token overrides when using model pricing: set
  `FIXED_PER_1K_INPUT_TOKENS` and/or `FIXED_PER_1K_OUTPUT_TOKENS`.
- Legacy envs are still accepted and mapped automatically:
  `MODEL_BASED_PRICING` → `!FIXED_PRICING`, `COST_PER_REQUEST` → `FIXED_COST_PER_REQUEST`,
  `COST_PER_1K_*` → `FIXED_PER_1K_*`.

Example fixed pricing:

```bash
FIXED_PRICING=true
FIXED_COST_PER_REQUEST=10
```

## Custom Models Configuration

Create a `models.json` file:

```json
{
  "models": [
    {
      "id": "gpt-4",
      "name": "GPT-4",
      "pricing": {
        "prompt": "0.00003",
        "completion": "0.00006",
        "request": "0"
      }
    },
    {
      "id": "gpt-3.5-turbo",
      "name": "GPT-3.5 Turbo",
      "pricing": {
        "prompt": "0.0000015",
        "completion": "0.000002",
        "request": "0"
      }
    }
  ]
}
```

## Security Best Practices

### Admin Password

Generate a strong password:

```bash
openssl rand -base64 32
```

### API Keys

- Rotate upstream API keys regularly
- Use read-only keys when possible
- Monitor key usage

### Network Security

- Restrict CORS origins in production
- Use HTTPS for public endpoints
- Enable Tor for anonymity

### Database Security

- Regular backups
- Encrypted storage volumes
- Restricted file permissions

## Troubleshooting

### Check Current Configuration

```bash
# View all environment variables
docker exec routstr env | sort

# Test configuration
curl http://localhost:8000/v1/info
```

### Common Issues

**Missing Upstream URL**

```
ERROR: UPSTREAM_BASE_URL not set
Solution: Set UPSTREAM_BASE_URL in .env
```

**Invalid Cashu Mint**

```
ERROR: Failed to connect to mint
Solution: Verify CASHU_MINTS URLs are accessible
```

**Database Errors**

```
ERROR: Database connection failed
Solution: Check DATABASE_URL and file permissions
```

## Advanced Configuration

### Multiple Mints

Configure fallback mints:

```bash
CASHU_MINTS=https://primary.mint,https://backup1.mint,https://backup2.mint
```

### Custom Database

Use PostgreSQL instead of SQLite:

```bash
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/routstr
```

### Proxy Settings

For corporate environments:

```bash
HTTP_PROXY=http://proxy.company.com:8080
HTTPS_PROXY=http://proxy.company.com:8080
```

## Next Steps

- [User Guide](../user-guide/introduction.md) - Start using Routstr
- [Admin Dashboard](../user-guide/admin-dashboard.md) - Manage your node
- [Custom Pricing](../advanced/custom-pricing.md) - Advanced pricing strategies
