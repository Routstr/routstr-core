# Admin Dashboard

The Admin Dashboard is your command center for managing your Routstr provider node. Configure providers, monitor earnings, manage models, and withdraw profits—all from a web interface.

**URL**: `http://your-node:8000/admin/`

---

## Overview Tab

The main dashboard view shows your node's financial status at a glance.

### Wallet Summary

| Metric | Description |
|--------|-------------|
| **Total Wallet** | All Bitcoin currently held by your node |
| **User Balances** | Funds belonging to active client sessions |
| **Your Balance** | Your profit: `Total - User Balances` |

### Mint Status

Shows connected Cashu mints and their balances. Each mint displays:

- Connection status
- Balance in sats/msats
- Unit type

<!-- TODO: Screenshot of Overview tab -->

---

## Sessions Tab

View and manage active client sessions (API keys).

### Session List

| Column | Description |
|--------|-------------|
| **Hashed Key** | Privacy-preserving identifier (not the actual key) |
| **Balance** | Remaining funds in the session |
| **Spent** | Total amount spent by this session |
| **Requests** | Number of API calls made |
| **Created** | When the session was created |
| **Expires** | Auto-expiry time (if set) |

### Actions

- **View Details** — See full session history
- **Revoke** — Terminate a session (remaining balance returns to your wallet)

<!-- TODO: Screenshot of Sessions tab -->

---

## Models Tab

Manage which AI models you offer to clients.

### Model List

Shows all models available from your upstream provider(s):

| Column | Description |
|--------|-------------|
| **Model ID** | The model identifier (e.g., `gpt-4o`) |
| **Enabled** | Whether clients can use this model |
| **Input Price** | Cost per 1M input tokens (USD) |
| **Output Price** | Cost per 1M output tokens (USD) |
| **Custom** | Whether pricing is overridden |

### Actions

- **Import Models** — Fetch latest model list from upstream
- **Enable/Disable** — Toggle model availability
- **Edit Pricing** — Override default pricing for a model
- **Create Alias** — Map a friendly name to a model

### Editing a Model

Click on any model to configure:

| Field | Description |
|-------|-------------|
| **Enabled** | Show this model to clients |
| **Prompt Price** | Custom price per 1M input tokens (USD) |
| **Completion Price** | Custom price per 1M output tokens (USD) |
| **Alias** | Alternative name for this model |

<!-- TODO: Screenshot of Models tab -->
<!-- TODO: Screenshot of Model edit modal -->

---

## Settings Tab

Configure all node settings. Changes take effect immediately.

### Upstream

Connect to your AI provider:

| Field | Description |
|-------|-------------|
| **Base URL** | API endpoint (e.g., `https://api.openai.com/v1`) |
| **API Key** | Your provider's secret key |

<!-- TODO: Screenshot of Upstream settings -->

### Node Identity

| Field | Description |
|-------|-------------|
| **Name** | Public display name |
| **Description** | Brief description of your service |
| **Npub** | Nostr public key for discovery |

### Pricing

| Field | Description |
|-------|-------------|
| **Fixed Pricing** | Toggle flat-rate vs. per-token pricing |
| **Fixed Cost** | Sats per request (when fixed pricing enabled) |
| **Exchange Fee** | Multiplier for BTC volatility buffer |
| **Upstream Fee** | Your profit margin multiplier |

**Example**: With Exchange Fee `1.005` and Upstream Fee `1.10`:

- Upstream cost: $30/1M tokens
- Your price: $30 × 1.005 × 1.10 = $33.17/1M tokens

### Cashu Mints

Manage which mints you accept payments from:

- **Add Mint** — Enter a mint URL
- **Remove Mint** — Stop accepting from a mint
- **Test Connection** — Verify mint is reachable

<!-- TODO: Screenshot of Mints settings -->

### Lightning

| Field | Description |
|-------|-------------|
| **Lightning Address** | Your LN address for automatic withdrawals |

### Nostr Discovery

| Field | Description |
|-------|-------------|
| **Nsec** | Private key for signing announcements |
| **Relays** | Where to publish your node advertisement |

### Security

| Field | Description |
|-------|-------------|
| **Admin Password** | Password for dashboard access |

!!! warning "Set a Password"
    The dashboard has no password by default. Always set one for production nodes.

<!-- TODO: Screenshot of Security settings -->

---

## Withdraw Tab

Withdraw your profits to a Lightning wallet.

### Steps

1. **Select Mint** — Choose which mint to withdraw from
2. **Enter Amount** — How many sats to withdraw
3. **Generate Token** — Creates a Cashu token
4. **Redeem** — Paste the token into your Cashu wallet and melt to Lightning

<!-- TODO: Screenshot of Withdraw tab -->

### Alternative: Lightning Address

If you've configured a Lightning Address in Settings, profits can be automatically swept to your wallet (coming soon).

---

## Logs Tab

View node logs for debugging without SSH access.

### Features

- **Filter by Level** — Error, Warning, Info, Debug
- **Search** — Find specific entries
- **Time Range** — View logs from specific periods
- **Auto-refresh** — Watch logs in real-time

### Common Log Entries

| Entry | Meaning |
|-------|---------|
| `Upstream request failed` | Problem connecting to your AI provider |
| `Invalid token` | Client sent an invalid Cashu token |
| `Session expired` | API key reached its time limit |
| `Insufficient balance` | Client ran out of funds mid-request |

<!-- TODO: Screenshot of Logs tab -->
