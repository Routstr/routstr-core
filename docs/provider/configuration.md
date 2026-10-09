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

Access the dashboard at `/admin` on your node.

### Upstream Providers

Connect to your AI provider(s):

| Setting          | Description                                      |
| ---------------- | ------------------------------------------------ |
| **Upstream URL** | API endpoint (e.g., `https://api.openai.com/v1`) |
| **API Key**      | Your provider's API key                          |

### DeepSeek

Choose **DeepSeek** as the provider type and paste an API key from
[platform.deepseek.com](https://platform.deepseek.com/api_keys); the base URL
is fixed to `https://api.deepseek.com`. Setting `DEEPSEEK_API_KEY` seeds the
provider on startup instead.

Models are listed from DeepSeek's own `/models` and priced from a rate table
in `routstr/upstream/deepseek.py`, not from litellm or OpenRouter:

- **Peak rates only.** DeepSeek charges half price off-peak, but the node bills
  one flat price per model, so it bills the peak rate. Clients overpay
  off-peak; the node never bills below cost. Time-of-day pricing is planned.
- **Unknown models import disabled.** A model DeepSeek lists that the table
  does not price shows up disabled in the Admin Dashboard. Enable it with a
  manual price, or add it to the table.
- **Cache hits** bill at DeepSeek's cache-hit rate (about 2% of the input
  rate on flash, about 3% on pro).

Thinking-mode `reasoning_content` is returned to clients unchanged in
responses, and forwarded unchanged when it appears in conversation history.
DeepSeek requires it on requests that carry `tools` and ignores it otherwise.

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

A fresh node ships with two mints preconfigured:

- `https://mint.minibits.cash/Bitcoin`
- `https://mint.cubabitcoin.org`

Setting `CASHU_MINTS` (env) or editing the list in the dashboard replaces this
default entirely. List order is significant: automatic foreign-mint swaps use
the first configured trusted mint. Core discovers that mint's active units from
its keysets and, when advertised, filters them through its enabled NUT-04/NUT-05
Bolt11 methods. For an existing key, its liability unit must remain supported;
for a new key, Core prefers the foreign token's unit, then `sat`, then `msat`.

### Confidential Upstream (optional)

Lets clients that opt in use your upstream provider *through* your node,
without your node seeing their prompts or responses, and without your
provider API key leaving your node. It needs the separate **cu-sidecar**
process ([routstr-confidential](https://github.com/jooray/routstr-confidential),
`crypto/relay`) running next to the node on loopback.

| Env | Description |
| --- | ----------- |
| `CONFIDENTIAL_SIDECAR_URL` | Base URL of the cu-sidecar (e.g. `http://127.0.0.1:7443`). Empty (default) disables the mode. |

Requires `NSEC` (the node signs its offer and receipts). Billing, mints and
refunds use your normal settings. See [Confidential upstream](../confidential-upstream.md).
With an empty trusted-mint list or no compatible unit, foreign top-ups are
rejected before any token proofs are spent.

#### Tokens from other mints

`/v1/wallet/topup` accepts tokens issued by mints outside this list by melting
them over Lightning into the first configured trusted mint. Bearer and X-Cashu
payments still refuse foreign mints (those paths run on every request and must
not wait on a third-party mint). A key is refunded on the mint that first
funded it: keys are always created with a trusted token or a Lightning
invoice, so a later foreign top-up does not move that mint. Keys with no
recorded mint that were first funded by a foreign top-up are refunded on that
foreign mint, swapped back net of fees. Safeguards:

- The token's mint URL must be HTTPS to a public address.
- Calls to the foreign mint get one attempt with a short deadline and share a
  process-wide concurrency cap, so a dead or hostile mint can only stall its own
  swap. They never run while the wallet lock is held.
- Swaps against the same foreign mint run one at a time. A request that finds
  one already running answers `cashu_swap_busy` (503 with `Retry-After`);
  nothing was spent, so the same token can be resent.
- Fees are quoted before anything is spent; a token that cannot cover them is
  refused with `cashu_foreign_mint_swap_failed` and stays spendable.
- Every swap is journaled in `cashu_swaps` before the Lightning leg. A timeout
  answers `cashu_swap_pending`; a background reconciler credits or fails the row
  once the mint confirms the outcome.

Lightning routing fees and the mint's input fees are deducted from the amount
credited (and from the refund). Following Cashu NUT-08 wallet behavior, any
unused inbound fee reserve is returned in the top-up response as
`change_token`, with `change_amount` and `change_unit`, and kept on the swap
row (`cashu_swaps.change_token`). The caller should store that token because it
remains redeemable on the foreign mint. Change is only returned on an immediate
200; swaps the reconciler finishes later do not build or return change.

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

To avoid configuring an identity by hand, set `AUTO_GENERATE_NSEC=true` and the
node creates one on first boot (see [Discovery](discovery.md)).

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
| `AUTO_GENERATE_NSEC` | Generate a Nostr identity on first boot when none is configured (stored encrypted, never printed; retrieve it with `scripts/reveal_nsec.py`; a provided `NSEC` wins) | `false` |
| `ENABLE_ANALYTICS_SHARING` | Enable usage analytics sharing to Nostr | `true`                         |
| `CASHU_MINTS`        | Comma-separated mint URLs         | `https://mint.minibits.cash/Bitcoin,https://mint.cubabitcoin.org` |
| `MINT_OPERATION_CONCURRENCY` | Concurrent mint/unit balance reads | `4` |
| `MINT_OPERATION_TIMEOUT_SECONDS` | Per-attempt timeout for mint network calls | `30` |
| `MINT_MAX_CONCURRENCY` | Concurrent operations allowed per mint (`0` disables the limit) | `4` |
| `MINT_RETRY_MAX_ATTEMPTS` | Retries after a timeout or HTTP 429 (`0` disables retries) | `3` |
| `FOREIGN_MINT_OPERATION_TIMEOUT_SECONDS` | Single-attempt deadline for calls to an unconfigured mint | `5` |
| `FOREIGN_MINT_MELT_TIMEOUT_SECONDS` | Deadline for a foreign mint to settle a Lightning melt; timeouts remain reconcilable and do not cool the mint | `60` |
| `FOREIGN_MINT_MAX_CONCURRENCY` | Process-wide cap on in-flight calls to unconfigured mints | `4` |
| `SWAP_RECONCILE_INTERVAL_SECONDS` | How often unfinished swaps are re-checked against their mints | `60` |
| `RECEIVE_LN_ADDRESS` | Lightning address for withdrawals | —                                    |
| `MIN_PAYOUT_SAT`     | Min payout balance in sats (applies to all mints) | `210`                |
| `MAX_PAYOUT_SAT`     | Maximum gross budget per periodic payout in sats, including fees (all mints) | `250000`             |
| `PAYOUT_INTERVAL_SECONDS` | Payout loop interval (seconds) | `900`                            |
| `TOR_PROXY_URL`      | SOCKS5 proxy for Tor              | `socks5://127.0.0.1:9050`            |
| `CORS_ORIGINS`       | Allowed CORS origins              | `*`                                  |
| `RELAYS`             | Nostr relays (comma-separated)    | (default set)                        |
| `MODEL_PATHS_REFRESH_INTERVAL_SECONDS` | How often to refresh `/v1/models/paths` discovery data; set `0` to pause the refresh (previously discovered paths keep being served) | `600` |
| `ENABLE_MODEL_PATHS_REFRESH` | Kill switch for the background model-path refresh (OpenRouter endpoint fan-out) | `true` |

Mint HTTP 429 responses create a per-mint cooldown. Operations that already hold
Routstr's wallet mutation lock fail fast during that cooldown instead of waiting
while blocking every other wallet mutation. Callers receive an error and may retry
later; the current response does not include the cooldown duration.

Read-only `/v1/checkstate` requests start at the SDK request-model limit
(currently 1,000 proofs) and adapt downward on HTTP 413 or 500, down to one
proof. A 500 is a size hypothesis, not a confirmed limit. Successful reduced
sizes are cached per mint within each worker for 24 hours (and refreshed while
in use). HTTP 429 never reduces the batch
size. Scan deadline expiry opens a transport cooldown without shortening any
existing rate-limit cooldown. Invalid, incomplete, or failed scans do
not produce a partial spendable balance. Only explicit UNSPENT proofs qualify;
PENDING proofs are retained but excluded from payouts.

Each scan is bounded by a fixed 60-second deadline and a 128-request budget;
exhausting either aborts that scan safely. Automatic splitting applies only to
state checks, **not swaps or melts**. Their limits are independent, and ambiguous
mutation outcomes must be reconciled rather than retried with different inputs.
Periodic payouts reload local proofs without forcing a keyset refresh, skip
state checks at/below `MIN_PAYOUT_SAT`, and cap each gross payout budget at
`MAX_PAYOUT_SAT`. Oversized inputs receive enough change outputs to return the
excess; they are not automatically swapped. The cap is not a proof-count limit
or a guarantee of Lightning payment success.

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

Model path discovery is refreshed in the background and exposed through
`/v1/models/paths`. The response groups each client-visible model ID with the
provider paths that may appear in chat-completion response metadata. Tune the
refresh cadence with `MODEL_PATHS_REFRESH_INTERVAL_SECONDS`.
