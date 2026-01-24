# Payment Flow

Routstr uses a **Pre-paid, Ephemeral** payment model. Pay first, use the funds, withdraw the rest. No accounts, no credit cards, no trails.

```
💰 Deposit  →  🤖 Use AI  →  💸 Withdraw Change
```

## 1. Creating a Balance (Deposit)

To start making requests, you must create a "Balance" (represented by an API Key).

### Method A: Lightning Network (Bolt11)

**Ideal for**: Users connecting from a standard Lightning wallet (Strike, Cash App, WoS).

1. **Request Invoice**:
    `POST /lightning/invoice` with `{"amount_sats": 5000, "purpose": "create"}`.
2. **Pay Invoice**: User scans and pays the QR code/bolt11 string.
3. **Receive Key**: Routstr detects the payment and issues a new API Key (`sk-...`) pre-loaded with 5,000 sats (5,000,000 msats).

### Method B: Cashu Token Import

**Ideal for**: Private, instant access or automated agents.

1. **Generate Token**: User creates a token in their local wallet (e.g., 1000 sats).
2. **Import**: `GET /v1/balance/create?initial_balance_token=cashuA...`
3. **Receive Key**: Routstr claims the token and issues an API Key (`sk-...`) with that balance.

---

## 2. Consuming Funds (Inference)

Every time you make a request to `/v1/chat/completions` (or others), the cost is deducted from your balance **in real-time**.

### Cost Calculation

`Cost = (Input_Tokens * Price_Input) + (Output_Tokens * Price_Output) + Request_Fee`

- Prices are defined per model (see `/v1/models`).
- If you stream the response, the balance is deducted incrementally or finalized at the end of the stream.
- If your balance hits 0 mid-stream, the connection is closed.

### Headers

Routstr checks the `Authorization: Bearer sk-...` header to identify which balance to charge.

---

## 3. Topping Up

If your balance runs low, you don't need a new key. You can top up the existing one.

### Via Lightning

`POST /lightning/invoice` with `{"amount_sats": 1000, "purpose": "topup", "api_key": "sk-..."}`.
*Once paid, the funds are added to your existing key.*

### Via Cashu

`POST /v1/balance/topup` with `{"cashu_token": "..."}` and `Authorization: Bearer sk-...`.

---

## 4. Refund (Withdrawal)

Don't leave large balances sitting on a node—it's a hot wallet. When you're done, get your sats back.

### Endpoint

`POST /v1/balance/refund`

**Headers**:
`Authorization: Bearer sk-...`

**Response**:

```json
{
  "token": "cashuAeyJ0b2tlbiI6W3sibWludCI6...",
  "msats": "450000"
}
```

You can verify the refund was successful by checking that the API Key is now invalid or has 0 balance. Copy the `token` string and paste it into your Cashu wallet to claim the Bitcoin.

---

## Summary

| Step | Action | Result |
|------|--------|--------|
| 💰 **Deposit** | Pay Lightning invoice or import Cashu | Get `sk-...` key |
| 🤖 **Use** | Make API requests | Balance decreases |
| 💸 **Refund** | Call `/v1/balance/refund` | Get Cashu token back |

That's it. No monthly bills, no surprise charges, no data harvesting.
