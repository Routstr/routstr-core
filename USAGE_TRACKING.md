# Usage Tracking System Documentation

This document describes the usage tracking system that monitors request volume, revenue, refunds, and errors by parsing application logs.

## Overview

The usage tracking system provides real-time analytics and historical data by parsing JSON-formatted log files from the `logs/` directory. It powers the admin dashboard at `/usage` with metrics like:

- Request volume (total, successful, failed)
- Revenue and refunds (in sats and msats)
- Error tracking and distributions
- Revenue breakdown by model
- Payment processing metrics

## Critical Log Messages

⚠️ **WARNING**: The following log messages are parsed by the usage tracking system. Do **NOT** modify or remove these messages without updating the parsing logic in `routstr/core/admin.py`.

### 1. Request Tracking

**Location**: `routstr/proxy.py:145`

```python
logger.info(
    "Received proxy request",
    extra={
        "method": request.method,
        "path": path,
        "client_host": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("user-agent", "unknown")[:100],
    },
)
```

**Usage**: Counts total incoming requests
**Parsed in**: `_aggregate_metrics_by_time()`, `_get_summary_stats()`, `get_revenue_by_model()`

### 2. Revenue Tracking (Successful Requests)

**Location**: `routstr/upstream/base.py:456` (streaming) and `routstr/upstream/base.py:559` (non-streaming)

```python
logger.info(
    "Token adjustment completed for streaming",  # or "...for non-streaming"
    extra={
        "key_hash": key.hashed_key[:8] + "...",
        "cost_data": cost_data,
        "balance_after_adjustment": fresh_key.balance,
    },
)
```

**Usage**: 
- Counts successful chat completions
- Extracts `cost_data.actual_cost` for revenue calculation (in msats)

**Required fields**:
- `cost_data` (dict) with `actual_cost` (int|float)

**Parsed in**: `_aggregate_metrics_by_time()`, `_get_summary_stats()`, `get_revenue_by_model()`

### 3. Payment Processing

**Location**: `routstr/auth.py:374`

```python
logger.info(
    "Payment processed successfully",
    extra={
        "key_hash": key.hashed_key[:8] + "...",
        "charged_amount": cost_per_request,
        "new_balance": key.balance,
        "total_spent": key.total_spent,
        "total_requests": key.total_requests,
    },
)
```

**Usage**: Counts successful payment processing events
**Parsed in**: `_aggregate_metrics_by_time()`, `_get_summary_stats()`

### 4. Refund Tracking (Failed Requests)

**Location**: `routstr/proxy.py:226`

```python
logger.warning(
    "Upstream request failed, revert payment",
    extra={
        "status_code": response.status_code,
        "path": path,
        "key_hash": key.hashed_key[:8] + "...",
        "key_balance": key.balance,
        "max_cost_for_model": max_cost_for_model,
        "upstream_headers": response.headers if hasattr(response, "headers") else None,
    },
)
```

**Usage**:
- Counts failed requests
- Extracts `max_cost_for_model` for refund calculation (in msats)

**Required fields**:
- `max_cost_for_model` (int|float)

**Parsed in**: `_aggregate_metrics_by_time()`, `_get_summary_stats()`, `get_revenue_by_model()`

### 5. Upstream Error Tracking

**Condition**: Any `ERROR` level log with "upstream" in the message

**Usage**: Counts upstream provider errors for reliability metrics
**Parsed in**: `_aggregate_metrics_by_time()`, `_get_summary_stats()`

## Usage Tracking Implementation

### Backend Endpoints

All usage tracking endpoints are in `routstr/core/admin.py`:

1. **`GET /admin/api/usage/metrics`**
   - Returns time-series data aggregated into intervals (5min, 15min, 30min, 1h)
   - Includes: requests, completions, failures, errors, warnings, payments, revenue, refunds

2. **`GET /admin/api/usage/summary`**
   - Returns aggregated statistics for the selected time period
   - Includes: totals, rates, revenue metrics, unique models, error distributions

3. **`GET /admin/api/usage/error-details`**
   - Returns detailed error logs with timestamps, messages, and locations

4. **`GET /admin/api/usage/revenue-by-model`**
   - Returns revenue breakdown by model with request counts and performance metrics

### Frontend Components

Located in `ui/`:

- **Page**: `app/usage/page.tsx` - Main usage tracking dashboard
- **Components**:
  - `components/usage-metrics-chart.tsx` - Line charts for time-series data
  - `components/usage-summary-cards.tsx` - Summary statistics cards
  - `components/error-details-table.tsx` - Error details table
  - `components/revenue-by-model-table.tsx` - Revenue by model table
- **API Services**: `lib/api/services/admin.ts` - TypeScript API client

### Key Parsing Functions

1. **`_parse_log_file(file_path: Path) -> list[dict]`**
   - Reads and parses JSON log entries from a file
   - Returns list of log entry dictionaries

2. **`_aggregate_metrics_by_time(entries, interval_minutes, hours_back)`**
   - Groups metrics into time buckets
   - Extracts revenue and refund amounts from log extras

3. **`_get_summary_stats(entries, hours_back)`**
   - Calculates aggregate statistics
   - Computes derived metrics (rates, averages, net revenue)

4. **`get_revenue_by_model(hours, limit)`**
   - Groups revenue/refund data by model ID
   - Calculates per-model performance metrics

## Modifying Log Messages

If you need to change any of the critical log messages:

1. Update the log statement in the source file
2. Update the parsing logic in `routstr/core/admin.py`:
   - `_aggregate_metrics_by_time()`
   - `_get_summary_stats()`
   - `get_revenue_by_model()`
3. Update the documentation in:
   - `routstr/core/logging.py` (module docstring)
   - This file (`USAGE_TRACKING.md`)
4. Test the changes with sample log data

## Log File Format

Logs are stored in `logs/app_YYYY-MM-DD.log` with JSON formatting:

```json
{
  "asctime": "2025-11-15 12:34:56",
  "name": "routstr.proxy",
  "levelname": "INFO",
  "message": "Token adjustment completed for non-streaming",
  "pathname": "/workspace/routstr/upstream/base.py",
  "lineno": 559,
  "version": "0.1.0",
  "request_id": "abc123",
  "model": "openai/gpt-4o-mini",
  "cost_data": {
    "actual_cost": 1234,
    "estimated_cost": 1500,
    "input_tokens": 100,
    "output_tokens": 50
  }
}
```

## Testing

To test usage tracking locally:

1. Generate some requests through the proxy
2. Check that logs are being written to `logs/app_YYYY-MM-DD.log`
3. Access the usage dashboard at `http://localhost:8000/usage` (requires admin authentication)
4. Verify metrics match the log entries

## Monitoring

- Logs rotate daily at midnight (UTC)
- 30 days of log history are kept by default
- Usage metrics refresh every 60 seconds in the UI
- API endpoints support 1-168 hours of history
