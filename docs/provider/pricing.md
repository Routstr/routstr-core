# Pricing

Routstr's pricing engine lets you act as a retailer of AI compute. You pay upstream providers (OpenAI, Anthropic, etc.) at their rates and sell to clients with your markup.

---

## Pricing Strategies

Configure these in **Dashboard** → **Settings** → **Pricing**.

### Dynamic Pricing (Default)

Passes through upstream costs plus your percentage markup.

**Formula**: `Client Price = Upstream Cost × Exchange Fee × Upstream Fee`

| Setting | Description | Default |
|---------|-------------|---------|
| **Exchange Fee** | Buffer for BTC price volatility | 1.005 (0.5%) |
| **Upstream Fee** | Your profit margin | 1.10 (10%) |

**Example**: GPT-4 costs $30/1M tokens from OpenAI. With default settings:

- Price: $30 × 1.005 × 1.10 = $33.17/1M tokens
- At $60k BTC: ~55,000 sats/1M tokens

### Fixed Pricing

Charge a flat rate per request, regardless of model or token count.

| Setting | Description |
|---------|-------------|
| **Fixed Pricing** | Enable flat-rate mode |
| **Fixed Cost** | Sats per request |

**Best for**: Simple proxies, internal tools, or subscription-like access.

---

## Per-Model Pricing

Override pricing for specific models in **Dashboard** → **Models**.

1. Click on a model
2. Enter custom **Prompt Price** and **Completion Price** (USD per 1M tokens)
3. Save

This overrides both the upstream cost and your global markup for that model.

**Example**: Lock GPT-4 at $35/1M tokens regardless of OpenAI's actual rate or your fee settings.

---

## Token-Based Overrides

Set global fixed rates per token (overrides dynamic pricing for all models):

| Setting | Description |
|---------|-------------|
| **Fixed Per 1K Input** | Sats per 1,000 prompt tokens |
| **Fixed Per 1K Output** | Sats per 1,000 completion tokens |

---

## Minimum Charge

Prevent spam with a minimum cost per request:

| Setting | Description | Default |
|---------|-------------|---------|
| **Min Request Cost** | Minimum charge in msats | 1000 (1 sat) |

If a request's calculated cost is lower than this, the client pays the minimum instead.

---

## Cost Tracking

Routstr tracks balances in **millisats (msats)** for precision with cheap models.

- 1 sat = 1,000 msats
- API responses include cost in msats
- Lightning withdrawals round down to whole sats

### Client Verification (RIP-05)

Clients can verify charges:

1. Fetch `/v1/models` for your advertised rates
2. Calculate expected cost from token counts
3. Compare to `x-routstr-cost` response header
