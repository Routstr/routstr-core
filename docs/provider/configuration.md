# Configuration

Routstr is configured primarily through the **Admin Dashboard**. All settings persist in the database and take effect immediately—no restarts required.

For automated deployments, you can optionally pre-configure settings via environment variables.

---

## Admin Dashboard (Primary)

Access the dashboard at `/admin/` on your node.

### Upstream Providers

Connect to your AI provider(s):

| Setting | Description |
|---------|-------------|
| **Upstream URL** | API endpoint (e.g., `https://api.openai.com/v1`) |
| **API Key** | Your provider's API key |

### Node Identity

How your node appears to clients:

| Setting | Description |
|---------|-------------|
| **Name** | Display name (e.g., "Fast GPT-4 Node") |
| **Description** | Brief description of your service |

### Pricing

Control your profit margins:

| Setting | Description | Default |
|---------|-------------|---------|
| **Fixed Pricing** | Charge flat rate per request vs. per-token | Off |
| **Exchange Fee** | Buffer for BTC volatility | 1.005 (0.5%) |
| **Upstream Fee** | Your profit markup | 1.10 (10%) |

See [Pricing](pricing.md) for detailed strategies.

### Cashu Mints

Which mints to accept payments from:

| Setting | Description |
|---------|-------------|
| **Mints** | List of trusted Cashu mint URLs |

### Lightning Withdrawals

Automatic profit withdrawal:

| Setting | Description |
|---------|-------------|
| **Lightning Address** | Your LN address for withdrawals |

### Security

| Setting | Description |
|---------|-------------|
| **Admin Password** | Password for dashboard access |

### Nostr Discovery

Announce your node on the network:

| Setting | Description |
|---------|-------------|
| **Npub** | Your Nostr public key |
| **Nsec** | Your Nostr private key (for signing) |
| **Relays** | Relays to publish announcements |

See [Discovery](discovery.md) for details.

---

## Environment Variables (Optional)

Use environment variables for:

- **Automated deployments** (CI/CD, infrastructure-as-code)
- **Secrets management** (external secret stores)
- **Initial bootstrap** (set once, manage via dashboard later)

### All Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `UPSTREAM_BASE_URL` | Upstream API endpoint | — |
| `UPSTREAM_API_KEY` | Upstream API key | — |
| `ADMIN_PASSWORD` | Dashboard password | (none) |
| `DATABASE_URL` | Database connection string | `sqlite+aiosqlite:///keys.db` |
| `NAME` | Node display name | `ARoutstrNode` |
| `DESCRIPTION` | Node description | `A Routstr Node` |
| `NPUB` | Nostr public key (bech32) | — |
| `NSEC` | Nostr private key | — |
| `CASHU_MINTS` | Comma-separated mint URLs | `https://mint.minibits.cash/Bitcoin` |
| `RECEIVE_LN_ADDRESS` | Lightning address for withdrawals | — |
| `TOR_PROXY_URL` | SOCKS5 proxy for Tor | `socks5://127.0.0.1:9050` |
| `CORS_ORIGINS` | Allowed CORS origins | `*` |
| `RELAYS` | Nostr relays (comma-separated) | (default set) |

### Priority

Environment variables are read on startup. Dashboard settings override them and persist in the database. Once you change a setting in the dashboard, the env var is ignored for that setting.

---

## Models

Manage which AI models you offer:

1. Go to **Models** in the dashboard
2. Models are auto-discovered from your upstream
3. For each model, you can:
   - **Enable/Disable** — hide expensive models you don't want to serve
   - **Override pricing** — set custom per-token rates
   - **Create aliases** — friendly names for models

See [Pricing](pricing.md) for per-model pricing strategies.
