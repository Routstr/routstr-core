# proxy

a reverse proxy that you can plug in front of any openai compatible api endpoint to handle payments using the cashu protocol (Bitcoin L3)

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UPSTREAM_BASE_URL` | **required** | Base URL for the upstream API service. |
| `UPSTREAM_API_KEY` | "" | API key for the upstream provider. |
| `COST_PER_REQUEST` | `1` | Base price per request in sats. |
| `COST_PER_1K_INPUT_TOKENS` | `0` | Price per 1k input tokens in sats. |
| `COST_PER_1K_OUTPUT_TOKENS` | `0` | Price per 1k output tokens in sats. |
| `MODEL_BASED_PRICING` | `false` | Enable pricing based on model metadata. |
| `RECEIVE_LN_ADDRESS` | **required** | Lightning address that receives payouts. |
| `MINT` | `https://mint.minibits.cash/Bitcoin` | Cashu mint URL. |
| `MINIMUM_PAYOUT` | `100` | Minimum sats before payouts are sent. |
| `REFUND_PROCESSING_INTERVAL` | `3600` | How often to check for expired keys (seconds). |
| `DEVS_DONATION_RATE` | `0.021` | Fraction of profit donated to developers. |
| `NSEC` | **required** | Nostr private key for the wallet. |
| `DATABASE_URL` | `sqlite+aiosqlite:///keys.db` | Database connection string. |
| `NAME` | `ARoutstrNode` | Name shown in API responses. |
| `DESCRIPTION` | `A Routstr Node` | Description shown in API responses. |
| `NPUB` | "" | Nostr public key for the node. |
| `HTTP_URL` | "" | Public HTTP URL for this node. |
| `ONION_URL` | "" | Onion address for this node. |
| `CORS_ORIGINS` | `*` | Allowed CORS origins separated by commas. |
| `EXCHANGE_FEE` | `1.005` | Spread used when fetching BTC prices. |
| `ADMIN_PASSWORD` | "" | Optional password for the admin dashboard. |
| `TOR_PROXY_URL` | `socks5://127.0.0.1:9050` | SOCKS5 proxy used for Tor requests. |
| `DEV_LN_ADDRESS` | `routstr@minibits.cash` | Address that receives developer donations. |

