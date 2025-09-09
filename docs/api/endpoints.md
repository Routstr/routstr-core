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

### Withdraw Funds

Withdraw balance as eCash.

```http
POST /v1/wallet/withdraw
Authorization: Bearer sk-...
```

**Request Body:**

```json
{
  "amount": 5000,
  "mint": "https://mint.example.com"
}
```

**Response:**

```json
{
  "cashu_token": "cashuAeyJ0...",
  "amount": 5000,
  "mint": "https://mint.example.com"
}
```

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
- [Examples](../user-guide/using-api.md) - Code examples
