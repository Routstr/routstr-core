# API Endpoints

Complete reference for all Routstr API endpoints.

## Overview

Routstr provides OpenAI-compatible endpoints with Bitcoin/eCash payment integration.

### Base URL

Choose a node from the [provider directory](https://routstr.com/providers) and
append `/v1` to its URL for the OpenAI-compatible base URL:

```text
https://provider.example/v1
```

`https://provider.example` is a placeholder; replace it with your chosen node's URL.
Check `/v1/models` before sending payment.


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

### Paid X-Cashu Settlement Failure

Paid `POST /v1/chat/completions` and `POST /v1/responses` requests can return
`503` with `X-Routstr-Error-Scope: node`, a top-level `request_id`,
`error.type = "internal_error"`, and
`error.code = "payment_settlement_failed"` when inference succeeded but the
node could not durably settle the payment, so the inference response is
withheld. The original `X-Cashu` token is already spent: do not resend it.
Keep the token and claim the stored refund with `POST /v1/balance/refund`.
For EHBP requests, this is a plaintext router-generated response without
`Ehbp-Response-Nonce`.

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
  Both are forwarded as upstream errors; retry with exponential backoff. The
  `429` keeps its status; the `529` is reported as `424`
  (see [Upstream attribution](errors.md#upstream-attribution-424-failed-dependency)).

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

## Decisions (OpenAI)

### Create Decision

Ask OpenAI's Decisions API (powered by `gpt-6-luna`) to pick from a finite set
of answers for text or image context. The API is in limited preview: accounts
that are not enrolled get `403 Decision API is not enabled for this user`.

```http
POST /v1/decisions
```

The request body is forwarded to `https://api.openai.com/v1/decisions`
unchanged; Routstr only reads the top-level `model` to route and price the
request, and settles from the response's `usage` like embeddings.

**Notes:**

- Only the `openai` provider serves this endpoint. A model that no OpenAI
  provider on the node offers returns `400 unsupported_request`, and a model
  the node does not list at all returns `400 invalid_model`. Use the model id
  from `/v1/models` (`gpt-6-luna`); the `openai/gpt-6-luna` alias resolves to
  OpenRouter's copy and returns `400 unsupported_request`.
- Pricing uses the node's catalog rate for the requested model.

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
          "path": "url=https%3A%2F%2Fapi.anthropic.com%2Fv1&model-id=anthropic%2Fclaude-sonnet-4",
          "provider": {"slug": "anthropic-primary", "type": "anthropic"},
          "endpoint": null
        },
        {
          "path": "url=https%3A%2F%2Fopenrouter.ai%2Fapi%2Fv1&model-id=anthropic%2Fclaude-sonnet-4&endpoint=google-vertex%2Fus",
          "provider": {"slug": "openrouter-main", "type": "openrouter"},
          "endpoint": {"tag": "google-vertex/us", "name": "Google"}
        }
      ]
    }
  ],
  "updated_at": 1753500000
}
```

`path` is an opaque, percent-encoded selector. Clients must store and return it
unchanged rather than parsing or reconstructing it. It identifies the route with
`url` and `model-id`. To avoid exposing private network details, a configured
private IP address or any URL with an explicit port is advertised as
`http://localhost`. OpenRouter routes additionally preserve the exact
machine-readable endpoint `tag`. Provider slugs/types and endpoint names remain
display data. When request-side selection is implemented, an endpoint tag must
not silently fall back to another backend.

A path names no provider, so several configured providers sharing an upstream URL
collapse onto a single path. Such a path always routes to the cheapest of those
providers, and the advertised slug, type and pricing describe that same cheapest
provider. A pinned path never fails over: if the selected provider errors, the
error is returned rather than retried elsewhere. Paths issued before this change
still carry `provider-id` and are still honoured, pinning the exact provider they
name.

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
  "msats": 10000000
}
```

For a foreign-mint token, unused NUT-08 Lightning fee reserve is returned to
the caller instead of being retained by the node:

```json
{
  "msats": 994000,
  "change_token": "cashuAeyJ0...",
  "change_amount": 3,
  "change_unit": "sat"
}
```

Store `change_token`; it remains redeemable on the mint that issued the input
token. The change fields are omitted when the mint returns no change. Change is
only returned when the swap completes within the request (HTTP 200); a swap
that answers 409 and is finished later by the node does not return change.

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

Returns the change token in the body and in an `X-Cashu` response header. Other answers:

| Status | Meaning |
|--------|---------|
| `404` | No request was paid with this token. |
| `425` | The request is still in flight, or its refund is being minted. Retry after `Retry-After` seconds. |
| `409` `token_already_spent` / `payment_fully_charged` | The request used the whole payment; no change is owed. Drop the token. |
| `409` `refund_failed` / `refund_unresolved` | Minting the refund failed. Contact the operator with the `payment_request_id` from the body (the paid request's id, not this call's `request_id`). |
| `409` `refund_unavailable` / `refund_not_recorded` | Paid before this node recorded refund outcomes; contact the operator with the `payment_request_id` from the body if change is owed. |
| `410` | The change was swept after going unclaimed. |

If the node stops while a paid request is in flight and before its charge is settled, the payment is refunded in full, less the mint's input fee for redeeming it: at once on a graceful shutdown, otherwise once the request lifetime the node had when the payment was accepted (`MAX_REQUEST_LIFETIME_SECONDS` plus `REQUEST_CLEANUP_TIMEOUT_SECONDS` and a minute) has passed. Until then the endpoint answers `425`. If a change refund was already minting after inference completed, that refund is awaited and the charge stands; a second refund is never minted. If the mint's outcome is unknown, the refund is not retried and the endpoint answers `409` `refund_unresolved` for manual reconciliation.

## Confidential Upstream

Present only when the node sets `CONFIDENTIAL_SIDECAR_URL`. The node speaks
protocol v3 only. This section lists the endpoints and messages; the money
rules, limits and settings are in
[Confidential upstream](../confidential-upstream.md), and the complete wire
format is §12 of `PROTOCOL-V3.md` in the routstr-confidential repository.

### Get Offer

```http
GET /v1/confidential/offer?v=3
```

The node's Nostr-signed offer (the query is ignored; clients send it). Fields:
`v` (`3`), `http` (the framings the sidecar serves now, `"2"` and/or `"1.1"`,
in the operator's order), `h2` (only when `http` lists `"2"`), `twopc`
(`"split-public-3"`), `connection_deposit_msat`, `idle_timeout_s`,
`evict_unused_after_s` (the minimum age of an unused connection before slot
pressure may close it, its deposit then charged; 0: never), `upstream_host`, `ws` (the session socket URL; relative when `HTTP_URL` is
unset), `head_template`, `key_length`, `key_alphabet`, `suffix_keys`, `chat`
(the pinned chat suffix), `max_tokens_cap`, `price_list` (per model `in`,
`cached_in`, `cache_write`, `out` in msats per 1k tokens, surcharge
included), `mints`, `notary_pubkey`, `notary_pubkey_curve`, and when set
`session_fee_msat`, `abandon_fee_msat`, `rate_surcharge_pct`,
`reserve_margin_tokens`, `upstream_models`, `context_lengths` (per chat
model, the prompt limit in tokens its reservation is sized by; absent for a
model without a known context length), `capabilities` + `images`.
`sig` is the node's BIP-340 signature and `sig_payload` the exact signed
bytes. A request may name an offer while the node serves it and for 10
minutes after.

Errors: `502` sidecar unavailable, `503` under `FIXED_PRICING`.

Models served this way carry an additive field in `GET /v1/models`:
`"confidential_upstream": {"v": 3, "offer_url": "…/v1/confidential/offer"[, "session_fee_msat", "abandon_fee_msat", "rate_surcharge_pct"][, "kind": "images"]}`.

### Connection

```http
GET /v1/confidential/ws?session_id=<uuid>&v=3   (WebSocket)
```

One **connection**: one two-party TLS handshake with the provider, carrying
one request (framing `"1.1"`) or many in turn (framing `"2"`). Control
messages are JSON text frames, TLS records binary frames. A socket without
`v=3`, or a `setup` without `"v": 3`, gets
`{"type":"error","status":426,"reason":"upgrade_required","min_v":3,…}` and
is closed.

**Setup** (first frame, within 10 s):

```json
{"type": "setup", "v": 3, "http": "2", "nonce_c": "<64 hex>", "armed": false,
 "auth": "sk-…|cashu…", "offer_sig": "<sig of the offer>"}
```

It names no request terms (a setup with `model`, `max_tokens`, `len`, … is
refused). The node checks that the sidecar serves `http`, that `offer_sig`
names an offer it signed, that the credential has funds, and reserves the
offer's connection deposit; only then it dials the sidecar. `ready`
(`{"type":"ready","v":3,"http","sid","nonce_n","armed",…}`) adds the node's
`pubkey`, `sig_scheme`, `zk_token` (for `/zk`) and `deposit_msat` (what was
reserved).

**Request** (framing `"2"`: after `head_written` and `settings_seen`;
framing `"1.1"`: once, before the ClientHello):

```json
{"type": "request", "n": 1, "kind": "chat", "model": "…", "max_tokens": 256,
 "body_len": 1834, "auth": "sk-…|cashu…", "offer_sig": "…",
 "session_fee_msat": 0, "abandon_fee_msat": 0}
```

Images: `"kind": "images"`, `variant` and `count` instead of `max_tokens`.
`n` is the last accepted request's number plus 1. `auth` and `offer_sig` may
differ from the setup's; the fee fields echo the named offer's. The node
reserves the model's full-context prompt (the named offer's
`context_lengths` entry) plus `max_tokens` (images:
`msat_per_image × count`) plus the larger fee, and forwards the request
without the credential. Answers:

- `{"type":"request_ok","sid","n","suffix",…,"reserved_msats"}`: accepted;
  `reserved_msats` is the most this request can be charged.
- `{"type":"request_refused","n","status","reason"[,"detail"]}`: nothing is
  held, the connection stays, the next request reuses `n`. `400` bad request
  or model, `401`/`402` credential or balance, `409` `offer_expired`,
  `offer_changed` or `session_fee_not_accepted`, `5xx` the node could not
  reserve. The ninth refusal in a row ends the connection.

Then the TLS records, π_WIN batches (`zk_params`, `release`) and
`body_released` (the provider has the complete request) pass through. The
request ends with one of:

- `disclose` (chat 2xx: the usage event, proven with π_C3S);
- `accept` (images 2xx);
- `disclose_head` (status ≥ 400: the response head);
- `disclose_reset` (framing `"2"`: the provider refused the stream unprocessed,
  `RST_STREAM(REFUSED_STREAM)` or GOAWAY `NO_ERROR`; settled like a 429).

The node re-signs the sidecar's `attestation` and answers the settlement with
`{"type":"receipt","receipt":{…},"receipt_json","sig","sig_scheme":"schnorr-secp256k1"}`.
Receipt fields: `v`, `sid`, `http`, `n`, `model` (`upstream_model` when the
provider's id differs), `usage`, `cost_msats`, `reserved_msats`,
`balance_msats`, `rates` (chat) or `kind`, `variant`, `count`,
`msat_per_image` (images), the offer's fees when set,
`deposit_released_msat` (the connection's first settlement),
`notary_pubkey`, `attestation_hash` and `time`. The node learns the status
line and headers of an error response, never its body.

**End.** The last frame is `{"type":"closed","reason"}` (`done`,
`error_settled`, `idle`, `max_requests`, `max_lifetime`, `max_bytes`,
`upstream_closed`, `evicted`, `ended`) or `error`. When the deposit was not
released by a settlement, it carries the signed outcome:
`deposit {sid, msat, outcome: "released"|"charged", reason, cost_msats, balance_msats, time}`,
`deposit_json`, `deposit_sig`, `sig_scheme`.

**Errors.** The node's own errors are
`{"type":"error","status":<int>,"reason":"…","detail":…}`, and the socket
closes: `400` bad setup or protocol error, `401`/`402` credential or balance
(`detail.error.type = "connection_deposit_unfunded"` when the deposit cannot
be reserved), `408` a time limit, `409` `offer_expired`, `413` frame too
large, `426` another protocol version, `429`/`503` connection limits, `503`
`wallet_busy` (a first-use Cashu token waited too long for the wallet lock;
retry), `502` sidecar unreachable or lost, `500` settlement failed. The
node's own failures carry `fault: "node"`; a sidecar `error` may carry
`fault` (`"upstream"`, `"node"`) and `code` (`upgrade_required`,
`twopc_version_mismatch`, `upstream_unreachable`, `h2_not_selected`,
`h2_withdrawn`, `h2_table_size`).

### Proof Channels

```http
GET /v1/confidential/zk?proof=<twopc|zkchan|pi_nk|pi_win|pi_c3s>&sid=<sid>&zk_token=<token>   (WebSocket)
```

A byte proxy to the sidecar: `twopc` (the two-party key schedule),
`zkchan` (the connection's proof channel), or one proof per socket.
`sid` and `zk_token` are those of `ready` (`session_id=<sid>` is accepted
in place of `sid`). Only a connection live on this node process is admitted,
with at most 6 sockets each; anything else is closed with code 1008.

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
