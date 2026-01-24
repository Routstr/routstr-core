# Advanced Pricing

Advanced pricing strategies for fine-tuned control over your revenue model.

---

## Default Behavior

By default, Routstr:

1. **Fetches costs** from your upstream provider
2. **Applies markup** using your fee settings
3. **Converts to sats** using real-time BTC price

**Formula**: `Price = Upstream Cost × Exchange Fee × Upstream Fee`

---

## Strategy 1: Fixed Per-Request

Charge a flat fee regardless of model or tokens used.

**Configure in Dashboard** → **Settings** → **Pricing**:

- Enable **Fixed Pricing**
- Set **Fixed Cost Per Request** (in sats)

**Use cases**:

- Internal tools with predictable usage
- Simple "pay once, get response" APIs
- Subscription-like tiers

---

## Strategy 2: Fixed Per-Token

Override dynamic pricing with global per-token rates.

**Configure in Dashboard** → **Settings** → **Pricing**:

| Setting | Description |
|---------|-------------|
| **Fixed Per 1K Input** | Sats per 1,000 prompt tokens |
| **Fixed Per 1K Output** | Sats per 1,000 completion tokens |

When set to non-zero values, these override model-specific pricing for all models.

---

## Strategy 3: Per-Model Custom Pricing

Set specific prices for individual models, overriding both upstream cost and global fees.

**Configure in Dashboard** → **Models**:

1. Click on a model (e.g., `gpt-4`)
2. Enter **Prompt Price** and **Completion Price** (USD per 1M tokens)
3. Save

**Example**: OpenAI charges $30/1M for GPT-4. Set your price to $35/1M to lock in a margin regardless of fee settings.

---

## Minimum Charge

Prevent dust transactions and spam:

| Setting | Description | Default |
|---------|-------------|---------|
| **Min Request Cost** | Minimum charge in msats | 1000 (1 sat) |

If a request's calculated cost falls below this (e.g., very short prompts), the client pays the minimum.

---

## Combining Strategies

Strategies apply in order of specificity:

1. **Per-model override** (highest priority)
2. **Fixed per-token rates**
3. **Dynamic pricing with fees** (default)
4. **Fixed per-request** (overrides all above if enabled)

**Example setup**:

- Dynamic pricing as default (10% markup)
- GPT-4 locked at $35/1M (premium model)
- Claude Haiku at 5 sats/1K tokens (budget option)
- Minimum 1 sat per request

---

## Pricing for Profit

### High-Volume Strategy

Lower margins, more clients:

- Exchange Fee: 1.002 (0.2%)
- Upstream Fee: 1.05 (5%)

### Premium Strategy

Higher margins, fewer clients:

- Exchange Fee: 1.01 (1%)
- Upstream Fee: 1.25 (25%)

### Mixed Strategy

- Cheap models (GLM-4.7-Flash, Seed-1.6): Low margin to attract volume
- Premium models (GPT-5-Pro, Claude-Opus): High margin for profit
