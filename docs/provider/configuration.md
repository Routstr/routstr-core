# Configuration

Routstr is configured primarily through the **Admin Dashboard**. All settings persist in the database and take effect immediately—no restarts required.

For automated deployments, you can optionally pre-configure settings via environment variables.

---

## Initial Setup (.env file)

Before running your node, you should create a `.env` file in the project root. This file is used to bootstrap the initial configuration and store sensitive secrets.

### Example .env

```bash
# Encrypts node secrets at rest. Optional — if unset, the node generates a key on
# first start and prints it once (back it up). Set it to manage the key yourself
# (recommended in production). See "Secrets at Rest" below.
ROUTSTR_SECRET_KEY=

# Node Identity
NAME="My AI Node"
DESCRIPTION="Fast access to models"

# Lightning Payouts
RECEIVE_LN_ADDRESS=yourname@wallet.com
```

### Setting the UI Password

On first start the node generates an admin password and logs it once — read it from the container logs to sign in. You can then change it two ways:

1.  **Via Dashboard**: Once logged in, go to **Settings** → **Security** to update your password.
2.  **Via Environment Variable (legacy seed)**: Setting `ADMIN_PASSWORD` in `.env` before the first start seeds the initial password instead of generating one. It's read only once, for existing deployments; a value left in `.env` is ignored after the node has been configured.

---

## Admin Dashboard (Primary)

Access the dashboard at `/admin/` on your node.

### Upstream Providers

Connect to your AI provider(s):

| Setting          | Description                                      |
| ---------------- | ------------------------------------------------ |
| **Upstream URL** | API endpoint (e.g., `https://api.openai.com/v1`) |
| **API Key**      | Your provider's API key                          |

### PPQ Auto Top-up

PPQ providers can automatically purchase more credits when their USD balance
falls below a configured threshold. Configure this per provider in the Admin
Dashboard by editing a **PPQ.AI** provider and opening **PPQ Auto Top-up**.
There are no environment variables for this feature.

#### Requirements

Before enabling auto top-up, make sure that:

- the PPQ provider has a valid API key;
- at least one trusted Cashu mint is configured;
- the node wallet has enough **node-owned** funds at one mint to pay the
  Lightning invoice; client balances are never used; and
- the node has a current BTC/USD price for validating the invoice amount.

| Setting | Description |
| ------- | ----------- |
| **Enable Auto Top-up** | Enables automatic PPQ credit purchases for this provider. |
| **When credits are below (USD)** | Starts a top-up when the reported PPQ balance is below this positive USD value. |
| **Purchase this amount (USD)** | Amount of PPQ credit to buy per top-up. Must be a whole number from **1 to 500 USD**. |

For example, a threshold of `5` and purchase amount of `20` buys 20 USD of
credit when the PPQ balance drops below 5 USD.

#### How it works

The worker checks eligible providers approximately once per minute. When the
balance is below the threshold, it:

1. verifies the node has enough owner funds before creating an invoice;
2. requests a USD-denominated Lightning top-up invoice from PPQ;
3. rejects expired, mismatched, or unexpectedly expensive invoices (more than
   10% above the local BTC/USD estimate);
4. pays from the configured Cashu mint with sufficient owner funds; and
5. waits for PPQ to confirm that the credit settled.

Only one attempt can be active for a provider. An attempt that was active at
the start of a cycle suppresses another top-up for that entire cycle, even if
PPQ reports it settled immediately. This prevents a temporarily stale PPQ
balance from causing a duplicate purchase.

Completed PPQ payments appear in the dashboard transaction history with source
`ppq_auto_topup`. The payment record is separate from the internal claim used
to prevent concurrent attempts.

#### Payment recovery

If the Cashu mint paid the invoice but PPQ settlement cannot be confirmed, the
provider card shows **Auto top-up needs review**. A payment still owned by a
running worker is shown as **Paying invoice** and cannot be released.

Before choosing **Release top-up**, manually verify both PPQ and the Cashu mint.
Release the claim only when the previous Lightning payment is definitively
unable to settle. Releasing an ambiguous payment allows the next cycle to try
again and can therefore cause a duplicate top-up.

Disabling auto top-up prevents new purchases, but the node continues to
reconcile an already active payment until it reaches a safe terminal state or
requires operator review.

### Node Identity

How your node appears to clients:

| Setting         | Description                            |
| --------------- | -------------------------------------- |
| **Name**        | Display name (e.g., "Fast GPT-4 Node") |
| **Description** | Brief description of your service      |

### Pricing

Control your profit margins:

| Setting           | Description                                | Default      |
| ----------------- | ------------------------------------------ | ------------ |
| **Fixed Pricing** | Charge flat rate per request vs. per-token | Off          |
| **Exchange Fee**  | Buffer for BTC volatility                  | 1.005 (0.5%) |
| **Upstream Fee**  | Your profit markup                         | 1.10 (10%)   |

See [Pricing](pricing.md) for detailed strategies.

### Cashu Mints

Which mints to accept payments from:

| Setting   | Description                     |
| --------- | ------------------------------- |
| **Mints** | List of trusted Cashu mint URLs |

### Lightning Withdrawals

Automatic profit withdrawal:

| Setting                              | Description                                                                                                | Default |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------- | ------- |
| **Lightning Address**                | Your LN address for withdrawals                                                                            | —       |
| **Minimum Payout (sat)**             | Min available balance (in sats) before profit is paid out. Applies to both `sat` and `msat` mints (auto-converted). | `210`   |
| **Payout Interval (seconds)**        | How often the payout loop wakes up and checks balances                                                     | `900`   |

All payout amounts must be positive. Set the minimums above your wallet's
minimum-invoice constraint (typically 1 sat) and high enough to amortise
routing fees.

### Security

| Setting            | Description                   |
| ------------------ | ----------------------------- |
| **Admin Password** | Password for dashboard access |

### Nostr Discovery

Announce your node on the network:

| Setting    | Description                          |
| ---------- | ------------------------------------ |
| **Npub**   | Your Nostr public key                |
| **Nsec**   | Your Nostr private key (for signing) |
| **Relays** | Relays to publish announcements      |
| **Share Analytics** | Publish aggregate usage stats to Nostr |

See [Discovery](discovery.md) for details.

---

## Environment Variables (Optional)

Use environment variables for:

- **Automated deployments** (CI/CD, infrastructure-as-code)
- **Secrets management** (external secret stores)
- **Initial bootstrap** (set once, manage via dashboard later)

### All Variables

| Variable             | Description                       | Default                              |
| -------------------- | --------------------------------- | ------------------------------------ |
| `UPSTREAM_BASE_URL`  | Upstream API endpoint             | —                                    |
| `UPSTREAM_API_KEY`   | Upstream API key                  | —                                    |
| `ADMIN_PASSWORD`     | Legacy seed for the dashboard password (otherwise generated + logged on first start) | (auto-generated) |
| `ROUTSTR_SECRET_KEY` | Master key encrypting node secrets at rest. Auto-generated to a key file if unset | (auto-generated) |
| `ROUTSTR_SECRET_KEY_FILE` | Path to the generated key file (used when `ROUTSTR_SECRET_KEY` is unset) | `routstr_secret.key` beside the database |
| `DATABASE_URL`       | Database connection string        | `sqlite+aiosqlite:///keys.db`        |
| `NAME`               | Node display name                 | `ARoutstrNode`                       |
| `DESCRIPTION`        | Node description                  | `A Routstr Node`                     |
| `NPUB`               | Nostr public key (bech32)         | —                                    |
| `NSEC`               | Legacy seed for the Nostr private key (otherwise set from the admin UI) | —                |
| `ENABLE_ANALYTICS_SHARING` | Enable usage analytics sharing to Nostr | `true`                         |
| `CASHU_MINTS`        | Comma-separated mint URLs         | `https://mint.minibits.cash/Bitcoin` |
| `RECEIVE_LN_ADDRESS` | Lightning address for withdrawals | —                                    |
| `MIN_PAYOUT_SAT`     | Min payout balance in sats (applies to all mints) | `210`                |
| `PAYOUT_INTERVAL_SECONDS` | Payout loop interval (seconds) | `900`                            |
| `TOR_PROXY_URL`      | SOCKS5 proxy for Tor              | `socks5://127.0.0.1:9050`            |
| `CORS_ORIGINS`       | Allowed CORS origins              | `*`                                  |
| `RELAYS`             | Nostr relays (comma-separated)    | (default set)                        |

### Priority

Environment variables are read on startup. Dashboard settings override them and persist in the database. Once you change a setting in the dashboard, the env var is ignored for that setting.

### Secrets at Rest

The node's Nostr private key (`nsec`) is encrypted in the database using
`ROUTSTR_SECRET_KEY`. You don't have to set it: if it's unset, the node generates a
key on first start, writes it **beside the database** (the file named by
`ROUTSTR_SECRET_KEY_FILE`, default `routstr_secret.key`) so it persists on the same
volume as your data, and prints it once.

**Back up that key** — it lives on the same volume as your database, so include it
in your backups. If it is lost or changed, previously encrypted secrets can't be
decrypted and must be re-entered — there is no rotation. To keep the key off the
data volume, set `ROUTSTR_SECRET_KEY` explicitly (an env value always takes
precedence over the file). See also [Deployment](deployment.md).

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
