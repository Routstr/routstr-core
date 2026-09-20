# API Endpoints

Complete reference for all Routstr API endpoints.

## Overview

Routstr provides OpenAI-compatible endpoints with Bitcoin/eCash payment integration.

### Base URL

All endpoints use the base URL:

```text
https://api.routstr.com/v1
```

### Authentication

All endpoints require authentication via:

- **Bearer Token**: `Authorization: Bearer sk-...` or `Authorization: Bearer cashuAeyJ0...`
- **X-Cashu Header**: `X-Cashu: cashuAeyJ0...` (for direct eCash payments)

See [Authentication](authentication.md) for details.

## Chat

### Create Chat Completion

Send messages to generate model responses.

```http
POST /v1/chat/completions
```

**Request Body:**

```json
{
  "model": "gpt-4",
  "messages": [
    {
      "role": "system",
      "content": "You are a helpful assistant."
    },
    {
      "role": "user",
      "content": "Hello!"
    }
  ],
  "temperature": 0.7,
  "stream": false
}
```

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `model` | string | Yes | - | Model ID to use |
| `messages` | array | Yes | - | Array of message objects |
| `temperature` | number | No | 1.0 | Sampling temperature (0-2) |
| `max_tokens` | integer | No | Model default | Maximum tokens to generate |
| `stream` | boolean | No | false | Stream partial responses |
| `top_p` | number | No | 1.0 | Nucleus sampling |
| `n` | integer | No | 1 | Number of completions |
| `stop` | string/array | No | null | Stop sequences |
| `presence_penalty` | number | No | 0 | Presence penalty (-2 to 2) |
| `frequency_penalty` | number | No | 0 | Frequency penalty (-2 to 2) |

**Response:**

```json
{
  "id": "chatcmpl-123",
  "object": "chat.completion",
  "created": 1677652288,
  "model": "gpt-4",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Hello! How can I help you today?"
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 13,
    "completion_tokens": 9,
    "total_tokens": 22
  }
}
```

### Streaming Response

When `stream: true`:

```text
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

## Completions (Coming Soon)

### Create Completion

**Note: This endpoint is coming soon and not yet available.**

Generate text completion (legacy endpoint).

```http
POST /v1/completions
```

**Request Body:**

```json
{
  "model": "gpt-3.5-turbo-instruct",
  "prompt": "Once upon a time",
  "max_tokens": 50,
  "temperature": 0.7
}
```

**Response:**

```json
{
  "id": "cmpl-123",
  "object": "text_completion",
  "created": 1677652288,
  "model": "gpt-3.5-turbo-instruct",
  "choices": [{
    "text": " in a faraway land, there lived a brave knight...",
    "index": 0,
    "logprobs": null,
    "finish_reason": "length"
  }],
  "usage": {
    "prompt_tokens": 4,
    "completion_tokens": 50,
    "total_tokens": 54
  }
}
```

## Embeddings

### Create Embeddings (Coming Soon)

**Note: This endpoint is coming soon and not yet available.**

Generate vector representations of text.

```http
POST /v1/embeddings
```

**Request Body:**

```json
{
  "model": "text-embedding-3-small",
  "input": "The quick brown fox jumps over the lazy dog",
  "encoding_format": "float"
}
```

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `model` | string | Yes | - | Embedding model ID |
| `input` | string/array | Yes | - | Text(s) to embed |
| `encoding_format` | string | No | "float" | Format: "float" or "base64" |
| `dimensions` | integer | No | Model default | Output dimensions |

**Response:**

```json
{
  "object": "list",
  "data": [{
    "object": "embedding",
    "index": 0,
    "embedding": [0.0023064255, -0.009327292, ...] 
  }],
  "model": "text-embedding-3-small",
  "usage": {
    "prompt_tokens": 9,
    "total_tokens": 9
  }
}
```

## System One (TypeSafe Decisions)

### Evaluate State

Evaluate a state against typed questions (noul / choice / score) on a TypeSafe
System One decision model (e.g. `jev-latest`). Requires a `typesafe` upstream
provider on the node.

```http
POST /v1/systemone
```

**Request Body:**

```json
{
  "model": "jev-latest",
  "state": "Help! My payouts have been failing for 3 days.",
  "questions": {
    "is_urgent": {
      "type": "noul",
      "instructions": "Does this convey urgency?"
    }
  }
}
```

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `model` | string | Yes | - | TypeSafe alias (`jev-latest`, `jev-preview`) or versioned id (`jev-1.13.0`) |
| `state` | string/object/array | Yes | - | Content to evaluate |
| `questions` | map<string, Question> | Yes | - | Typed questions; answers keyed identically |

**Response:**

```json
{
  "model": "jev-latest",
  "answers": {
    "is_urgent": {
      "type": "noul",
      "noul": 0.95
    }
  },
  "usage": {
    "input_tokens": 312,
    "output_tokens": 48
  }
}
```

Billing is input-token based (output tokens are free on Jev); the response's
`usage` is the settlement seam, exactly like embeddings.

**Notes:**

- The response `model` echoes the id you requested (e.g. `jev-latest`), not the
  resolved build (`jev-1.13.0`) TypeSafe returns. Routstr also adds its standard
  `id`, `cost`, `metadata.routstr` and `usage.*_msats` fields.
- TypeSafe's `GET /v1/models` lists aliases only; the node additionally seeds
  the known versioned ids so they can be requested directly.
- TypeSafe answers `429 Too Many Requests` and `529 Overloaded` when throttled.
  Both are forwarded as upstream errors; retry with exponential backoff.

**Enabling the provider:**

1. Admin UI → Providers → *TypeSafe* (base URL is fixed to
   `https://api.typesafe.ai/v1`), paste your `api.typesafe.ai` key. Or
   `POST /admin/api/upstream-providers` with
   `{"provider_type": "typesafe", "api_key": "<key>"}`.
2. On a node with an empty provider table, setting `TYPESAFE_API_KEY` seeds
   the provider automatically.
3. `jev-latest`, `jev-preview` and `jev-1.13.0` are catalogued with the
   published rate ($0.042 per million input tokens, output free). Override the
   model row if TypeSafe changes pricing.

## Images (Coming Soon)

### Create Image

**Note: This endpoint is coming soon and not yet available.**

Generate images from text prompts.

```http
POST /v1/images/generations
```

**Request Body:**

```json
{
  "model": "dall-e-3",
  "prompt": "A white siamese cat wearing a space helmet",
  "n": 1,
  "size": "1024x1024",
  "quality": "standard"
}
```

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `model` | string | Yes | - | Model: dall-e-2, dall-e-3 |
| `prompt` | string | Yes | - | Text description |
| `n` | integer | No | 1 | Number of images |
| `size` | string | No | "1024x1024" | Image dimensions |
| `quality` | string | No | "standard" | Quality: standard, hd |
| `style` | string | No | "vivid" | Style: vivid, natural |
| `response_format` | string | No | "url" | Format: url, b64_json |

**Response:**

```json
{
  "created": 1677652288,
  "data": [{
    "url": "https://generated-image-url.com/image.png",
    "revised_prompt": "A white Siamese cat wearing a detailed space helmet..."
  }]
}
```

## Audio (Coming Soon)

### Create Transcription

**Note: This endpoint is coming soon and not yet available.**

Convert audio to text.

```http
POST /v1/audio/transcriptions
Content-Type: multipart/form-data
```

**Form Data:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | Yes | Audio file (mp3, mp4, mpeg, mpga, m4a, wav, webm) |
| `model` | string | Yes | Model ID (whisper-1) |
| `language` | string | No | Language code (ISO-639-1) |
| `prompt` | string | No | Context prompt |
| `response_format` | string | No | Format: json, text, srt, verbose_json, vtt |
| `temperature` | number | No | Sampling temperature |

**Response:**

```json
{
  "text": "Hello, this is the transcribed audio content."
}
```

### Create Translation

**Note: This endpoint is coming soon and not yet available.**

Translate audio to English.

```http
POST /v1/audio/translations
Content-Type: multipart/form-data
```

Same parameters as transcription, but always translates to English.

## Models

### List Models

Get available models and pricing.

```http
GET /v1/models
```

**Response:**

```json
{
  "object": "list",
  "data": [
    {
      "id": "gpt-3.5-turbo",
      "object": "model",
      "created": 1677610602,
      "owned_by": "openai",
      "permission": [...],
      "root": "gpt-3.5-turbo",
      "parent": null,
      "pricing": {
        "prompt": 0.001,
        "completion": 0.002,
        "unit": "1k tokens"
      }
    }
  ]
}
```

### List Model Paths

Get the selectable upstream routes for each advertised model. This endpoint is
discovery-only; request-side selection will be added separately.

```http
GET /v1/models/paths
```

**Response:**

```json
{
  "data": [
    {
      "id": "anthropic/claude-sonnet-4",
      "paths": [
        {
          "path": "url=https%3A%2F%2Fapi.anthropic.com%2Fv1&provider-id=12&model-id=anthropic%2Fclaude-sonnet-4",
          "provider": {"id": 12, "slug": "anthropic-primary", "type": "anthropic"},
          "endpoint": null
        },
        {
          "path": "url=https%3A%2F%2Fopenrouter.ai%2Fapi%2Fv1&provider-id=42&model-id=anthropic%2Fclaude-sonnet-4&endpoint=google-vertex%2Fus",
          "provider": {"id": 42, "slug": "openrouter-main", "type": "openrouter"},
          "endpoint": {"tag": "google-vertex/us", "name": "Google"}
        }
      ]
    }
  ],
  "updated_at": 1753500000
}
```

`path` is an opaque, percent-encoded selector. Clients must store and return it
unchanged rather than parsing or reconstructing it. It identifies the exact
configured route with `url`, `provider-id`, and `model-id`. To avoid exposing
private network details, a configured private IP address or any URL with an
explicit port is advertised as `http://localhost`. OpenRouter routes additionally
preserve the exact machine-readable endpoint `tag`. Provider slugs/types and
endpoint names remain display data. When request-side selection is implemented,
an endpoint tag must not silently fall back to another backend.

### List Paths for One Model

Use the exact model ID advertised by `/v1/models`. The query parameter safely
supports IDs containing `/`.

```http
GET /v1/models/paths/model?model_id=anthropic/claude-sonnet-4
```

The response uses the same path objects and `updated_at` field as the collection
endpoint. An unknown model returns `404 Model not found`. A known model whose
paths have not been discovered yet returns `200` with an empty `data` array.

## Wallet Management

### Create Wallet (Coming Soon)

**Note: This endpoint is coming soon. Currently, you can use Cashu tokens directly as API keys.**

Create a new wallet with eCash deposit.

```http
POST /v1/wallet/create
```

**Request Body:**

```json
{
  "cashu_token": "cashuAeyJ0...",
  "admin_key": "optional-admin-key"
}
```

**Response:**

```json
{
  "api_key": "sk-1234567890abcdef",
  "admin_key": "radmin_fedcba0987654321",
  "balance": 10000,
  "mint": "https://mint.example.com",
  "unit": "sat"
}
```

### Get Key Information

Get current balance and consumption data for an API key.

```http
GET /v1/balance/info
Authorization: Bearer sk-...
```

**Response:**

```json
{
  "api_key": "sk-abc...",
  "balance": 8500000,
  "reserved": 0,
  "total_requests": 42,
  "total_spent": 1500000,
  "validity_date": null
}
```

`balance` is the spendable balance used by request admission.

### Check Balance

Get current wallet balance.

```http
GET /v1/wallet/balance
Authorization: Bearer sk-...
```

**Response:**

```json
{
  "balance": 8500,
  "currency": "sat",
  "reserved": 0
}
```

### Top Up Wallet

Add funds to existing wallet.

```http
POST /v1/wallet/topup
Authorization: Bearer sk-...
```

**Request Body:**

```json
{
  "cashu_token": "cashuAeyJ0..."
}
```

**Response:**

```json
{
  "balance": 18500,
  "amount_added": 10000,
  "currency": "sat"
}
```

### Refund Balance

Pay out the current balance. The key remains valid at zero balance and can be topped up again. The payout goes to a Lightning address when one is given (in the request or stored on the key), otherwise a Cashu token is returned.

```http
POST /v1/balance/refund
Authorization: Bearer sk-...
Content-Type: application/json
```

`/v1/wallet/refund` is a deprecated alias.

**Request Body** (optional):

```json
{
  "lightning_address": "user@getalby.com"
}
```

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `lightning_address` | string | No | Key's stored refund address | Lightning address or LNURL to pay. Overrides the stored address for this request. The effective address (request or stored) is resolved only for a request that can open a new claim, before any balance is debited. |

**Response (Lightning):**

```json
{
  "refund_id": "3f9c1e2d8b7a4c6e9f0a1b2c3d4e5f60",
  "status": "paid",
  "recipient": "user@getalby.com",
  "sats": "4500"
}
```

**Response (Cashu):**

```json
{
  "refund_id": "3f9c1e2d8b7a4c6e9f0a1b2c3d4e5f60",
  "status": "paid",
  "token": "cashuAeyJ0...",
  "sats": "4500"
}
```

The amount field is `sats` or `msats` depending on the key's refund currency. It reports the gross balance debited by the claim. For Lightning refunds, mint and input fees can reduce the amount actually delivered to the recipient.

**Behaviour:**

- The balance is debited and a refund claim is recorded before the payout is attempted. A key has at most one open claim at a time.
- If the payout fails cleanly, the claim is closed and the balance is restored. Retry the request.
- Once a melt quote has been recorded or a Cashu token has been issued, the payout may already have happened, so any later failure returns `502` and withholds the balance rather than restoring it. The exception is the mint answering the melt itself with `unpaid`: that is proof nothing was sent, so the balance is restored at once and the request returns `503`.
- If the Lightning payment is dispatched but the mint cannot confirm the outcome, the request returns `502`, the balance stays withheld, and a background reconciler asks the mint until it answers. The balance is restored if the mint reports the payment unpaid.
- An unresolved claim is reported before any replay: a request on a key with an open claim returns `409` with that claim's `refund_id` and `status`.
- Calling again on a zero-balance key with no open claim returns the last paid Lightning refund, or the Cashu token issued by the last paid claim while it remains uncollected.

**Errors:**

| Status | Meaning |
|--------|---------|
| `400` | Invalid Lightning destination, no balance, or balance too small for the refund unit |
| `400` | Ongoing requests are still reserving balance on this key |
| `401` | Unknown key |
| `409` | Balance changed concurrently. Retry. |
| `409` | `refund_in_progress`: another refund claim for this key is still open. The body carries its `refund_id` and `status` |
| `409` | `refund_unresolved`: a claim for this key is `stuck` and needs operator reconciliation |
| `410` | Previously issued Cashu refund token has been swept |
| `500` | Payout failed before anything was dispatched. Balance restored. Retry. |
| `502` | Payment dispatched, outcome unconfirmed. Balance withheld pending reconciliation. Do not retry. |
| `503` | Mint unavailable, or the mint reported the Lightning payment unpaid. Balance restored. Retry later. |

**X-Cashu refunds:**

Requests paid per-call with an `X-Cashu` header get their change from this endpoint by sending the same header instead of `Authorization`:

```http
POST /v1/balance/refund
X-Cashu: cashuAeyJ0...
```

Returns the change token in the body and in an `X-Cashu` response header. `404` if no matching request exists, `425` while the change is still being minted, `410` if it was swept.

## Provider Discovery

## Admin Settings

These endpoints are protected by the Admin cookie (`admin_password` set to your configured admin password).

### Get Settings

```http
GET /admin/api/settings
```

Returns the current application settings (sensitive values may be redacted).

### Update Settings

```http
PATCH /admin/api/settings
Content-Type: application/json
```

Body is a partial JSON of settings fields to update. Validated and persisted to the database.

### List Providers

Get available upstream providers.

```http
GET /v1/providers
```

**Response:**

```json
{
  "providers": [
    {
      "name": "openai",
      "models": ["gpt-4", "gpt-3.5-turbo"],
      "endpoints": ["chat/completions", "completions"],
      "status": "active"
    }
  ]
}
```

### Provider Info

Get specific provider details.

```http
GET /v1/providers/{provider_name}
```

**Response:**

```json
{
  "name": "openai",
  "display_name": "OpenAI",
  "description": "Official OpenAI API",
  "models": [
    {
      "id": "gpt-4",
      "name": "GPT-4",
      "context_window": 8192,
      "pricing": {
        "prompt": 0.03,
        "completion": 0.06,
        "unit": "1k tokens"
      }
    }
  ],
  "endpoints": ["chat/completions", "completions", "embeddings"],
  "features": ["streaming", "function_calling"],
  "status": "active"
}
```

## Rate Limiting

All endpoints are subject to rate limiting:

- **Per minute**: 60 requests
- **Per hour**: 1000 requests
- **Per day**: 10000 requests

Rate limit information is included in response headers.

## Next Steps

- [Errors](errors.md) - Error handling reference
- [Authentication](authentication.md) - Auth details
- [Integration Guide](../client/integration.md) - Code examples
