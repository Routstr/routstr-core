# Node Admin API Specification

Version: 1.0.0

## Overview

This document defines the REST API specification for third-party admin dashboards to configure and monitor Routstr proxy nodes. The API provides endpoints for authentication, wallet management, balance monitoring, and administrative operations.

## Base URL

```text
https://{node-domain}/v1/admin
```

## Authentication

The API uses JWT (JSON Web Token) authentication. All endpoints except `/auth/login` require a valid JWT token.

### JWT Token Structure

```json
{
  "sub": "admin",
  "iat": 1234567890,
  "exp": 1234571490,
  "role": "admin"
}
```

### Authentication Header

```text
Authorization: Bearer {jwt_token}
```

## Error Responses

All error responses follow this format:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable error message",
    "details": {} // Optional additional error details
  }
}
```

Common HTTP status codes:

- `401` - Unauthorized (invalid or missing token)
- `403` - Forbidden (insufficient permissions)
- `400` - Bad Request (invalid parameters)
- `404` - Not Found
- `500` - Internal Server Error

## Endpoints

### Authentication Endpoints

#### POST /auth/login

Authenticate and receive a JWT token.

**Request Body:**

```json
{
  "password": "admin_password"
}
```

**Response (200 OK):**

```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_at": "2024-01-20T15:30:00Z",
  "token_type": "Bearer"
}
```

**Response (401 Unauthorized):**

```json
{
  "error": {
    "code": "INVALID_CREDENTIALS",
    "message": "Invalid password"
  }
}
```

#### POST /auth/refresh

Refresh an existing JWT token before expiration.

**Request Header:**

```text
Authorization: Bearer {current_jwt_token}
```

**Response (200 OK):**

```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_at": "2024-01-20T15:30:00Z",
  "token_type": "Bearer"
}
```

### Wallet Information

#### GET /wallet/balance

Get comprehensive wallet balance information.

**Response (200 OK):**

```json
{
  "summary": {
    "owner_balance_sats": 150000,
    "total_wallet_balance_sats": 500000,
    "total_user_balance_sats": 350000,
    "last_updated": "2024-01-20T10:00:00Z"
  },
  "details": [
    {
      "mint_url": "https://mint.example.com",
      "unit": "sat",
      "wallet_balance": 300000,
      "user_balance": 200000,
      "owner_balance": 100000,
      "status": "active"
    },
    {
      "mint_url": "https://anothermint.example.com",
      "unit": "usd",
      "wallet_balance": 5000,
      "user_balance": 3000,
      "owner_balance": 2000,
      "status": "active"
    },
    {
      "mint_url": "https://erroredmint.example.com",
      "unit": "sat",
      "error": "Connection timeout",
      "status": "error"
    }
  ]
}
```

#### GET /wallet/balance/{mint_url}

Get balance information for a specific mint.

**Path Parameters:**

- `mint_url` - URL-encoded mint URL

**Query Parameters:**

- `unit` - (optional) Currency unit filter (e.g., "sat", "usd")

**Response (200 OK):**

```json
{
  "mint_url": "https://mint.example.com",
  "units": [
    {
      "unit": "sat",
      "wallet_balance": 300000,
      "user_balance": 200000,
      "owner_balance": 100000,
      "last_synced": "2024-01-20T09:55:00Z"
    }
  ]
}
```

### Withdrawal Operations

Manage withdrawals from the node's wallet balance.

#### POST /wallet/withdraw

Create a withdrawal token for the specified amount.

**Request Body:**

```json
{
  "amount": 50000,
  "mint_url": "https://mint.example.com",
  "unit": "sat"
}
```

**Response (200 OK):**

```json
{
  "token": "cashuAey...",
  "amount": 50000,
  "unit": "sat",
  "mint_url": "https://mint.example.com",
  "created_at": "2024-01-20T10:15:00Z",
  "transaction_id": "txn_123456"
}
```

**Response (400 Bad Request):**

```json
{
  "error": {
    "code": "INSUFFICIENT_BALANCE",
    "message": "Insufficient wallet balance",
    "details": {
      "requested": 50000,
      "available": 30000
    }
  }
}
```

### API Key Management

#### GET /keys

Get all API keys and their balances.

**Query Parameters:**

- `status` - (optional) Filter by status: "active", "expired", "all" (default: "all")
- `limit` - (optional) Number of records (default: 50, max: 100)
- `offset` - (optional) Pagination offset (default: 0)

**Response (200 OK):**

```json
{
  "api_keys": [
    {
      "hashed_key": "sha256:a1b2c3...",
      "balance_msats": 100000000,
      "total_spent_msats": 50000000,
      "total_requests": 1523,
      "refund_address": "cashuAey...",
      "created_at": "2024-01-01T00:00:00Z",
      "expires_at": "2024-12-31T23:59:59Z",
      "status": "active"
    }
  ],
  "pagination": {
    "total": 25,
    "limit": 50,
    "offset": 0
  }
}
```

#### POST /keys

Create a new API key.

**Request Body:**

```json
{
  "initial_balance_msats": 100000000,
  "expiry_hours": 720,
  "refund_address": "cashuAey..."
}
```

**Response (201 Created):**

```json
{
  "api_key": "routstr_live_abc123...",
  "hashed_key": "sha256:a1b2c3...",
  "balance_msats": 100000000,
  "expires_at": "2024-02-19T10:00:00Z"
}
```

#### DELETE /keys/{hashed_key}

Delete/revoke an API key.

**Path Parameters:**

- `hashed_key` - The hashed API key identifier

**Response (200 OK):**

```json
{
  "message": "API key revoked successfully",
  "refunded_amount_msats": 50000000
}
```

### Logging and Monitoring

#### GET /logs

Search and retrieve logs.

**Query Parameters:**

- `request_id` - (optional) Specific request ID to search for
- `level` - (optional) Log level filter: "ERROR", "WARNING", "INFO", "DEBUG"
- `from_time` - (optional) ISO 8601 timestamp
- `to_time` - (optional) ISO 8601 timestamp
- `limit` - (optional) Number of entries (default: 100, max: 1000)
- `offset` - (optional) Pagination offset

**Response (200 OK):**

```json
{
  "logs": [
    {
      "timestamp": "2024-01-20T10:00:00.123Z",
      "level": "ERROR",
      "request_id": "req_123456",
      "message": "Failed to connect to mint",
      "context": {
        "mint_url": "https://mint.example.com",
        "error_code": "CONNECTION_TIMEOUT"
      },
      "source": {
        "file": "wallet.py",
        "line": 145,
        "function": "connect_to_mint"
      }
    }
  ],
  "pagination": {
    "total": 523,
    "limit": 100,
    "offset": 0
  }
}
```

#### GET /logs/{request_id}

Get all logs for a specific request ID.

**Path Parameters:**

- `request_id` - The request ID to investigate

**Response (200 OK):**

```json
{
  "request_id": "req_123456",
  "logs": [
    {
      "timestamp": "2024-01-20T10:00:00.123Z",
      "level": "INFO",
      "message": "Processing payment request",
      "context": {
        "amount": 1000,
        "unit": "sat"
      }
    }
  ],
  "summary": {
    "total_logs": 15,
    "error_count": 2,
    "warning_count": 1,
    "duration_ms": 245
  }
}
```

### System Status

#### GET /status

Get system health and status information.

**Response (200 OK):**

```json
{
  "status": "healthy",
  "version": "1.2.3",
  "uptime_seconds": 864000,
  "last_health_check": "2024-01-20T10:00:00Z",
  "mints": [
    {
      "url": "https://mint.minibits.cash/Bitcoin",
      "status": "connected",
      "units": ["sat", "usd"],
      "last_sync": "2024-01-20T09:59:55Z"
    }
  ],
  "database": {
    "status": "connected",
    "active_api_keys": 25,
    "total_requests_today": 1523
  },
  "admin_password_set": true
}
```

### Configuration

#### GET /config

Get current node configuration. Sensitive values like API keys are redacted. The response includes all configurable environment variables.

**Response (200 OK):**

```json
{
  "node_info": {
    "name": "ARoutstrNode v1.2.3",
    "description": "A Routstr Node",
    "npub": "npub1...",
    "http_url": "https://node.example.com",
    "onion_url": "http://example.onion"
  },
  "cashu_mints": [
    "https://mint.minibits.cash/Bitcoin"
  ],
  "receive_ln_address": "lightning:address@provider.com",
  "pricing": {
    "cost_per_request_msats": 1000,
    "cost_per_1k_input_tokens_msats": 0,
    "cost_per_1k_output_tokens_msats": 0,
    "model_based_pricing": false,
    "exchange_fee": 1.005,
    "upstream_provider_fee": 1.05
  },
  "features": {
    "cors_origins": ["*"],
    "log_level": "INFO",
    "enable_console_logging": true
  },
  "upstream": {
    "base_url": "https://openrouter.ai/api/v1",
    "upstream_base_url": "",
    "has_upstream_api_key": false
  }
}
```

## Pagination

All list endpoints support pagination using `limit` and `offset` parameters. The response includes pagination metadata:

```json
{
  "data": [...],
  "pagination": {
    "total": 250,
    "limit": 50,
    "offset": 0,
    "has_more": true
  }
}
```

## Versioning

The API uses URL path versioning. The current version is `v1`. When breaking changes are introduced, a new version will be created while maintaining the previous version for backward compatibility.

## Security Considerations

1. All API communication must use HTTPS
2. JWT tokens should have a reasonable expiration time (recommended: 1 hour)
3. Store API keys and passwords securely (hashed)
4. Implement IP allowlisting for admin API access
5. Log all administrative actions for audit trails
