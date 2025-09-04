# Error Handling

This guide covers error responses, codes, and handling strategies for the Routstr API.

## Error Response Format

All errors follow a consistent JSON structure:

```json
{
  "error": {
    "type": "error_type",
    "message": "Human-readable error message",
    "code": "error_code",
    "details": {
      "additional": "context-specific information"
    }
  }
}
```

## HTTP Status Codes

| Status | Meaning | Common Causes |
|--------|---------|---------------|
| 400 | Bad Request | Invalid parameters, malformed JSON |
| 401 | Unauthorized | Invalid or missing API key |
| 402 | Payment Required | Insufficient balance |
| 403 | Forbidden | Access denied to resource |
| 404 | Not Found | Endpoint or resource doesn't exist |
| 422 | Unprocessable Entity | Validation errors |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Server-side error |
| 502 | Bad Gateway | Upstream API error |
| 503 | Service Unavailable | Temporary outage |

## Error Types

### Authentication Errors

#### Invalid API Key

```json
{
  "error": {
    "type": "authentication_failed",
    "message": "Invalid API key provided",
    "code": "invalid_api_key"
  }
}
```

**Status:** 401  
**Resolution:** Check API key format and validity

#### Expired API Key

```json
{
  "error": {
    "type": "authentication_failed",
    "message": "API key has expired",
    "code": "key_expired",
    "details": {
      "expired_at": "2024-01-01T00:00:00Z",
      "refund_available": true
    }
  }
}
```

**Status:** 401  
**Resolution:** Create new API key or contact admin for refund

#### Missing Authorization

```json
{
  "error": {
    "type": "authentication_failed",
    "message": "Authorization header required",
    "code": "missing_auth"
  }
}
```

**Status:** 401  
**Resolution:** Include `Authorization: Bearer {api_key}` header

### Payment Errors

#### Insufficient Balance

```json
{
  "error": {
    "type": "insufficient_balance",
    "message": "Insufficient balance for request",
    "code": "payment_required",
    "details": {
      "balance": 100,
      "required": 154,
      "shortfall": 54,
      "estimated_tokens": {
        "prompt": 50,
        "completion": 150
      }
    }
  }
}
```

**Status:** 402  
**Resolution:** Top up API key balance

#### Invalid Token

```json
{
  "error": {
    "type": "payment_error",
    "message": "Invalid Cashu token",
    "code": "invalid_token",
    "details": {
      "reason": "Token already spent"
    }
  }
}
```

**Status:** 400  
**Resolution:** Use a valid, unspent token

#### Mint Unavailable

```json
{
  "error": {
    "type": "payment_error",
    "message": "Cannot connect to Cashu mint",
    "code": "mint_unavailable",
    "details": {
      "mint_url": "https://mint.example.com",
      "retry_after": 60
    }
  }
}
```

**Status:** 503  
**Resolution:** Try again later or use different mint

### Validation Errors

#### Invalid Parameters

```json
{
  "error": {
    "type": "invalid_request",
    "message": "Invalid request parameters",
    "code": "validation_error",
    "details": {
      "errors": [
        {
          "field": "temperature",
          "message": "Must be between 0 and 2",
          "value": 3.5
        },
        {
          "field": "model",
          "message": "Model 'gpt-5' not found",
          "value": "gpt-5"
        }
      ]
    }
  }
}
```

**Status:** 422  
**Resolution:** Fix parameter values

#### Missing Required Fields

```json
{
  "error": {
    "type": "invalid_request",
    "message": "Missing required fields",
    "code": "missing_fields",
    "details": {
      "missing": ["model", "messages"]
    }
  }
}
```

**Status:** 400  
**Resolution:** Include all required fields

### Rate Limiting

#### Rate Limit Exceeded

```json
{
  "error": {
    "type": "rate_limit_exceeded",
    "message": "Too many requests",
    "code": "rate_limit",
    "details": {
      "limit": 100,
      "window": "1 minute",
      "retry_after": 45
    }
  }
}
```

**Status:** 429  
**Headers:**

```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1640995200
Retry-After: 45
```

**Resolution:** Wait for retry_after seconds

### Upstream Errors

#### Model Overloaded

```json
{
  "error": {
    "type": "upstream_error",
    "message": "Model is currently overloaded",
    "code": "model_overloaded",
    "details": {
      "model": "gpt-4",
      "retry_after": 5
    }
  }
}
```

**Status:** 503  
**Resolution:** Retry request after delay

#### Upstream Timeout

```json
{
  "error": {
    "type": "upstream_error",
    "message": "Request to upstream API timed out",
    "code": "upstream_timeout",
    "details": {
      "timeout": 30,
      "endpoint": "chat/completions"
    }
  }
}
```

**Status:** 504  
**Resolution:** Retry with shorter prompt or max_tokens

### Content Policy

#### Content Filtered

```json
{
  "error": {
    "type": "content_policy_violation",
    "message": "Content filtered due to policy violation",
    "code": "content_filtered",
    "details": {
      "reason": "harmful_content",
      "categories": ["violence", "hate"]
    }
  }
}
```

**Status:** 400  
**Resolution:** Modify prompt to comply with policies

## Error Handling Best Practices

### Retry Logic

Implement exponential backoff with jitter:

```python
import time
import random
from typing import Optional, Callable

def retry_with_backoff(
    func: Callable,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0
) -> Optional[Any]:
    """Retry function with exponential backoff."""
    
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            
            # Check if error is retryable
            if hasattr(e, 'status_code'):
                if e.status_code in [429, 502, 503, 504]:
                    # Calculate delay with jitter
                    delay = min(
                        base_delay * (2 ** attempt) + random.uniform(0, 1),
                        max_delay
                    )
                    
                    # Use retry_after if provided
                    if hasattr(e, 'retry_after'):
                        delay = e.retry_after
                    
                    time.sleep(delay)
                else:
                    # Non-retryable error
                    raise
```

### Error Categories

Group errors for handling:

```python
class ErrorHandler:
    # Errors that should be retried
    RETRYABLE_ERRORS = {
        'rate_limit',
        'upstream_timeout',
        'model_overloaded',
        'mint_unavailable'
    }
    
    # Errors requiring user action
    USER_ACTION_ERRORS = {
        'insufficient_balance',
        'invalid_api_key',
        'key_expired'
    }
    
    # Errors requiring code changes
    CLIENT_ERRORS = {
        'validation_error',
        'missing_fields',
        'invalid_request'
    }
    
    @classmethod
    def handle_error(cls, error_response: dict) -> None:
        error_code = error_response['error']['code']
        
        if error_code in cls.RETRYABLE_ERRORS:
            # Implement retry logic
            pass
        elif error_code in cls.USER_ACTION_ERRORS:
            # Alert user
            pass
        elif error_code in cls.CLIENT_ERRORS:
            # Log for debugging
            pass
```

### Graceful Degradation

Handle errors without breaking application flow:

```python
async def get_ai_response(prompt: str) -> str:
    """Get AI response with fallback handling."""
    try:
        # Try primary model
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    
    except InsufficientBalanceError:
        # Fall back to cheaper model
        try:
            response = await client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100  # Limit tokens
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"Fallback failed: {e}")
            return "Service temporarily unavailable"
    
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return "An error occurred processing your request"
```

### Logging Errors

Structure error logs for debugging:

```python
import logging
import json

def log_api_error(error_response: dict, context: dict) -> None:
    """Log API errors with context."""
    logger = logging.getLogger(__name__)
    
    error_data = {
        'timestamp': datetime.utcnow().isoformat(),
        'error': error_response['error'],
        'context': {
            'endpoint': context.get('endpoint'),
            'api_key_id': context.get('api_key_id'),
            'request_id': context.get('request_id'),
            'model': context.get('model')
        }
    }
    
    logger.error(
        "API Error",
        extra={'structured_data': json.dumps(error_data)}
    )
```

### User-Friendly Messages

Map technical errors to user messages:

```python
ERROR_MESSAGES = {
    'insufficient_balance': "Your account balance is too low. Please add funds to continue.",
    'invalid_api_key': "Invalid API key. Please check your configuration.",
    'rate_limit': "Too many requests. Please wait a moment and try again.",
    'model_overloaded': "The AI service is busy. Please try again in a few seconds.",
    'validation_error': "Invalid request. Please check your input and try again."
}

def get_user_message(error_code: str) -> str:
    """Get user-friendly error message."""
    return ERROR_MESSAGES.get(
        error_code,
        "An unexpected error occurred. Please try again later."
    )
```

## Common Scenarios

### Handling Balance Errors

```python
async def make_request_with_balance_check():
    try:
        # Check balance first
        balance_info = await client.get("/v1/wallet/balance")
        
        # Estimate cost
        estimated_cost = calculate_cost(model, prompt_length)
        
        if balance_info['balance'] < estimated_cost * 1.1:  # 10% buffer
            # Proactively top up
            await top_up_balance()
        
        # Make request
        return await client.chat.completions.create(...)
        
    except InsufficientBalanceError as e:
        # Handle insufficient balance
        shortfall = e.details['shortfall']
        await top_up_balance(amount=shortfall * 2)
        # Retry request
```

### Handling Rate Limits

```python
from datetime import datetime, timedelta

class RateLimitTracker:
    def __init__(self):
        self.reset_times = {}
    
    def is_limited(self, endpoint: str) -> bool:
        reset_time = self.reset_times.get(endpoint)
        if reset_time and datetime.now() < reset_time:
            return True
        return False
    
    def set_limit(self, endpoint: str, reset_timestamp: int):
        self.reset_times[endpoint] = datetime.fromtimestamp(reset_timestamp)
    
    def wait_time(self, endpoint: str) -> float:
        reset_time = self.reset_times.get(endpoint)
        if reset_time:
            return max(0, (reset_time - datetime.now()).total_seconds())
        return 0
```

## Testing Error Handling

### Unit Tests

```python
import pytest
from unittest.mock import Mock

async def test_insufficient_balance_handling():
    # Mock API client
    mock_client = Mock()
    mock_client.chat.completions.create.side_effect = InsufficientBalanceError(
        required=100,
        available=50
    )
    
    # Test error handling
    handler = ErrorHandler(mock_client)
    result = await handler.safe_request(
        model="gpt-4",
        messages=[{"role": "user", "content": "test"}]
    )
    
    # Verify fallback behavior
    assert result.fallback_used is True
    assert result.model == "gpt-3.5-turbo"
```

### Integration Tests

```python
async def test_real_error_scenarios():
    # Test with invalid API key
    invalid_client = OpenAI(
        api_key="sk-invalid",
        base_url=test_url
    )
    
    with pytest.raises(AuthenticationError) as exc_info:
        await invalid_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "test"}]
        )
    
    assert exc_info.value.status_code == 401
    assert "invalid_api_key" in str(exc_info.value)
```

## Monitoring Errors

Track error rates and patterns:

```python
class ErrorMetrics:
    def __init__(self):
        self.error_counts = defaultdict(int)
        self.error_timestamps = defaultdict(list)
    
    def record_error(self, error_code: str):
        self.error_counts[error_code] += 1
        self.error_timestamps[error_code].append(datetime.now())
    
    def get_error_rate(self, error_code: str, window_minutes: int = 60) -> float:
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        recent_errors = [
            ts for ts in self.error_timestamps[error_code]
            if ts > cutoff
        ]
        return len(recent_errors) / window_minutes
```

## Next Steps

- [Authentication](authentication.md) - Auth error details
- [Endpoints](endpoints.md) - Endpoint-specific errors
- [Examples](../user-guide/using-api.md) - Error handling examples
