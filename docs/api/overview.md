# API Reference Overview

Routstr Core provides a complete OpenAI-compatible API with additional endpoints for payment management. This reference covers all available endpoints, authentication methods, and response formats.

## Base URL

```
https://api.routstr.com/v1
```

All API endpoints are prefixed with `/v1` for versioning.

## Authentication

Routstr uses API keys for authentication. Include your key in the Authorization header:

```bash
Authorization: Bearer sk-...
```

### API Key Format

- Prefix: `sk-`
- Length: 32 characters
- Example: `sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p`

### Cashu Tokens as Authentication

You can also use a Cashu eCash token directly in the `Authorization` header. The server hashes the token internally; this hash represents your API key identity and carries the token's balance.

```bash
Authorization: Bearer cashuAeyJ0b2tlbiI6W3...
```

## Content Types

### Request

- **Required**: `Content-Type: application/json`
- **Encoding**: UTF-8
- **Maximum Size**: 10MB (configurable)

### Response

- **Type**: `application/json` or `text/event-stream` (for streaming)
- **Encoding**: UTF-8
- **Compression**: gzip (if accepted)

## Rate Limiting

Rate limits are applied per API key:

- **Requests**: 1000 per minute
- **Tokens**: 1,000,000 per hour
- **Concurrent**: 10 simultaneous requests

Rate limit headers:

```
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 999
X-RateLimit-Reset: 1640995200
```

## Error Responses

All errors follow a consistent format:

```json
{
  "error": {
    "type": "insufficient_balance",
    "message": "Insufficient balance for request",
    "code": "payment_required",
    "details": {
      "required": 154,
      "available": 100
    }
  }
}
```

### Error Types

| Type | Status Code | Description |
|------|-------------|-------------|
| `invalid_request` | 400 | Malformed request |
| `authentication_failed` | 401 | Invalid or missing API key |
| `insufficient_balance` | 402 | Not enough balance |
| `forbidden` | 403 | Access denied |
| `not_found` | 404 | Resource not found |
| `rate_limit_exceeded` | 429 | Too many requests |
| `internal_error` | 500 | Server error |
| `upstream_error` | 502 | Upstream API error |

## Endpoint Categories

### AI/ML Endpoints

Standard OpenAI-compatible endpoints:

- **Models**: `/v1/models`
- **Responses**: `/v1/responses`
- **Chat Completions**: `/v1/chat/completions`
- **Embeddings**: `/v1/embeddings`
- **Completions**: `/v1/completions` *(planned)*
- **Images**: `/v1/images/generations` *(planned)*
- **Audio**: `/v1/audio/transcriptions` *(planned)*

### Payment Endpoints

Routstr-specific payment management:

- **Balance**: `/v1/balance/*`
- **Node Info**: `/v1/info`

### Admin Endpoints

Protected administrative functions:

- **Dashboard**: `/admin/`
- **API Management**: `/admin/api/*`

## Request Headers

### Standard Headers

| Header | Required | Description |
|--------|----------|-------------|
| `Authorization` | Yes | Bearer token with API key |
| `Content-Type` | Yes | Must be `application/json` |
| `Accept` | No | Response format preference |
| `Accept-Encoding` | No | Compression support |
| `X-Request-ID` | No | Client-provided request ID |

### Custom Headers

| Header | Description |
|--------|-------------|
| `X-Cashu` | eCash token for per-request payment |

#### X-Cashu: Stateless Per-Request Payment

Instead of using `Authorization: Bearer sk-...`, you can send a Cashu token directly in the `X-Cashu` header. The response will include an `X-Cashu-Refund` header with your change.

```bash
curl https://api.routstr.com/v1/chat/completions \
  -H "X-Cashu: cashuA3s8jKx9..." \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello"}]}'
```

The response includes your change in the same header:
```
X-Cashu: cashuA7k2mNp4...
```

This is fully stateless—no session, no `/v1/balance/refund` call needed. However, **streaming does not work with `X-Cashu`** because the refund can only be calculated after the full response is generated.

## Response Headers

### Standard Headers

| Header | Description |
|--------|-------------|
| `Content-Type` | Response format |
| `Content-Length` | Response size |
| `X-Request-ID` | Unique request identifier |
| `X-Cashu` | Change token (when request used `X-Cashu` header) |

## Streaming Responses

For endpoints supporting streaming, responses use Server-Sent Events:

```
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1694268190,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1694268190,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":" there"},"finish_reason":null}]}

data: [DONE]
```

## OpenAPI Specification

The complete OpenAPI 3.0 specification is available at:

```
GET /openapi.json
```

Interactive documentation:

```
GET /docs        # Swagger UI
GET /redoc       # ReDoc
```

## SDK Support

Routstr is compatible with official OpenAI SDKs:

### Python

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-...",
    base_url="https://your-node.com/v1"
)
```

### JavaScript/TypeScript

```javascript
import OpenAI from 'openai';

const openai = new OpenAI({
    apiKey: 'sk-...',
    baseURL: 'https://your-node.com/v1'
});
```

### cURL

```bash
curl https://your-node.com/v1/chat/completions \
  -H "Authorization: Bearer sk-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"Hello"}]}'
```

## Webhook Support

Configure webhooks for events:

```json
POST /v1/webhooks
{
  "url": "https://your-app.com/webhook",
  "events": ["balance.low", "key.expired"],
  "secret": "whsec_your_secret"
}
```

Events are sent with signature verification:

```
X-Webhook-Signature: sha256=...
```

## API Versioning

- Current version: `v1`
- Version in URL path: `/v1/endpoint`

## Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 201 | Created |
| 204 | No content |
| 400 | Bad request |
| 401 | Unauthorized |
| 402 | Payment required |
| 403 | Forbidden |
| 404 | Not found |
| 429 | Rate limited |
| 500 | Server error |
| 502 | Upstream error |
| 503 | Service unavailable |

## CORS Support

CORS is enabled with configurable origins:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS
Access-Control-Allow-Headers: Authorization, Content-Type
Access-Control-Max-Age: 86400
```

## Compression

Responses are compressed with gzip when:

- Client sends `Accept-Encoding: gzip`
- Response is larger than 1KB
- Content type is compressible

## Batch Requests *(planned)*

Process multiple operations in one request. Coming soon.

## Node Info

Get node metadata:

```
GET /v1/info
```

Supported models and pricing are available at `/v1/models`.

## Next Steps

- [Authentication](authentication.md) - Detailed auth guide
- [Endpoints](endpoints.md) - Complete endpoint reference
- [Errors](errors.md) - Error handling guide
- [Integration Guide](../client/integration.md) - Code examples
