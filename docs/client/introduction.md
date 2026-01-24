# Introduction to Routstr

Welcome to the Routstr Core User Guide. This guide will help you understand how to use Routstr to access AI APIs with Bitcoin micropayments.

## What You'll Learn

- How the payment system works (Cashu eCash)
- Creating and managing API keys (Ephemeral Sessions)
- Making API calls through Routstr
- Using the admin dashboard

## Prerequisites

### 💰 Wallet

Cashu ([cashu.me](https://cashu.me)) or Lightning ([Strike](https://strike.me), Cash App, etc.)

### 🌐 Provider

A Routstr node, e.g. `https://api.routstr.com`

### 🤖 Client

OpenAI SDK, Claude Code, Cursor, or any OpenAI-compatible tool

---

## How Routstr Works

Routstr is a **Payment Proxy**. It sits between your code and the AI provider.

### Traditional API vs Routstr

| Traditional | Routstr |
|---|---|
| Credit Card Required | Bitcoin / Lightning / eCash |
| Monthly Billing | Pay-per-request (Real-time) |
| KYC / Account | No Account / Private |
| Single Provider | Aggregated Providers |

### Key Concepts

#### 1. Cashu eCash

Digital bearer tokens backed by Bitcoin. They are instant, private, and have no fees for internal transfers. Routstr uses these tokens as the "credits" for API requests.

#### 2. Ephemeral Sessions (API Keys)

Instead of a permanent account, you create a **Session**.

- You fund a session with eCash or Lightning.
- Routstr gives you an `api_key` (`sk-...`) representing that session.
- You use the `api_key` until funds run out or you finish your task.
- You can **refund** the remaining balance back to your wallet at any time.

#### 3. Millisats (msats)

Everything is priced in **millisatoshis**.

- 1 Satoshi (sat) = 1,000 msats.
- This allows for extremely precise pricing (e.g., 0.05 sats per prompt).

---

## Workflow: Zero to Intelligence

### 1. Fund a Session

You need an `api_key` with a balance.

**Easiest: Use the Web UI**
Visit the node's root page (e.g., [api.routstr.com](https://api.routstr.com)) or [chat.routstr.com](https://chat.routstr.com) → Settings to create a key visually with Lightning.

**Option A: Lightning Invoice (CLI)**
Generate an invoice and pay it with any Lightning wallet.

```bash
curl -X POST https://api.routstr.com/lightning/invoice \
  -d '{"amount_sats": 1000, "purpose": "create"}'
```

*Returns an invoice (`bolt11`) and an ID. Once paid, the status endpoint returns your `api_key`.*

**Option B: Cashu Token (Best for privacy & devs)**
If you have a Cashu wallet, you can copy a token string (`cashuA...`) and use it directly.

- **Direct Usage**: Use the token *as* your API key in the `Authorization` header.
- **Import**: Or exchange it for a standard `sk-...` key:

```bash
curl "https://api.routstr.com/v1/balance/create?initial_balance_token=cashuA..."
```

*Returns your `api_key` immediately.*

### 2. Configure Your Client

Use the standard OpenAI SDK, just changing the `base_url` and `api_key`.

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.routstr.com/v1",
    api_key="sk-7f8e9d..."  # The key from Step 1
)
```

### 3. Make Requests

```python
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Explain quantum computing."}]
)
```

### 4. Withdraw Change

When you are done, get your change back as a Cashu token.

```bash
curl -X POST https://api.routstr.com/v1/balance/refund \
  -H "Authorization: Bearer sk-7f8e9d..."
```

*Returns a `token` that you can paste back into Nutstash or Minibits to reclaim your funds.*

---

## Supported Features

- **Responses**: `/v1/responses` (OpenAI Responses API)
- **Chat Completions**: `/v1/chat/completions` (Streaming supported)
- **Embeddings**: `/v1/embeddings`
- **Models**: `/v1/models` (List available models and prices)

## Next Steps

- **[Payment Flow](payments.md)**: Detailed breakdown of the funding lifecycle.
- **[Models & Pricing](../provider/pricing.md)**: How costs are calculated.
- **[Admin Dashboard](../provider/dashboard.md)**: Managing your node if you are the operator.
