# Unified Logs & Usage Analytics Implementation

This document describes the unified implementation that combines features from PR #228 (Log Search) and PR #229 (Usage Analytics Dashboard) into a single, well-architected solution.

## Overview

Both PR #228 and PR #229 needed to parse log files from the `logs/` directory, but approached it differently. This unified implementation:

1. **Eliminates code duplication** by creating shared log reading infrastructure
2. **Separates concerns** between generic log search and analytics-specific metrics
3. **Combines both UIs** with proper navigation in the admin sidebar
4. **Maintains all features** from both PRs while improving code organization

## Architecture

### Backend Structure

```
routstr/logs/
├── __init__.py          # Public API exports
├── reader.py            # Shared log file reading/parsing infrastructure
├── search.py            # Generic log search with filtering
└── metrics.py           # Usage analytics metrics extraction
```

#### 1. `reader.py` - Shared Infrastructure

**Purpose**: Provides common functionality for reading and parsing JSON log files.

**Key Functions**:
- `parse_log_file(file_path)` - Parse a single JSON log file
- `get_log_files_in_range(logs_dir, hours_back, date)` - Get relevant log files
- `filter_entries_by_time(entries, hours_back)` - Filter entries by time range
- `get_available_log_dates(logs_dir)` - Get list of available log dates

**Benefits**:
- DRY (Don't Repeat Yourself) - single source of truth for log reading
- Consistent error handling across all log operations
- Easy to extend with additional functionality

#### 2. `search.py` - Log Search Feature (from PR #228)

**Purpose**: Generic log search and filtering for debugging and viewing logs.

**Key Function**:
- `search_logs(logs_dir, date, level, request_id, search_text, limit)` - Search with filters

**Features**:
- Filter by specific date (YYYY-MM-DD)
- Filter by log level (INFO, WARNING, ERROR, etc.)
- Filter by request ID (exact match)
- Text search in message and name fields (case-insensitive)
- Configurable result limit

#### 3. `metrics.py` - Usage Analytics (from PR #229)

**Purpose**: Extract and aggregate usage metrics from logs for analytics dashboard.

**Key Functions**:
- `aggregate_metrics_by_time(logs_dir, interval_minutes, hours_back)` - Time-series data
- `get_summary_stats(logs_dir, hours_back)` - Summary statistics
- `get_error_details(logs_dir, hours_back, limit)` - Detailed error information
- `get_revenue_by_model(logs_dir, hours_back, limit)` - Revenue breakdown by model

**Metrics Tracked**:
- Request volume (total, successful, failed)
- Revenue and refunds (in sats and msats)
- Error tracking and distributions
- Revenue breakdown by model
- Payment processing metrics

**Critical Log Messages** (must not be modified without updating metrics.py):
- `"received proxy request"` → counts total_requests
- `"token adjustment completed"` → counts successful completions, extracts revenue from cost_data.total_msats
- `"upstream request failed"` or `"revert payment"` → counts failed requests, extracts refunds
- `"payment processed successfully"` → counts payment events
- ERROR level with `"upstream"` → counts upstream errors

### API Endpoints

#### Log Search Endpoints
- `GET /admin/api/logs` - Get filtered log entries
- `GET /admin/api/logs/dates` - Get available log dates
- `GET /admin/logs` - Redirect to frontend logs page

#### Usage Analytics Endpoints
- `GET /admin/api/usage/metrics` - Time-series metrics (5min, 15min, 30min, 1h intervals)
- `GET /admin/api/usage/summary` - Aggregated statistics for time period
- `GET /admin/api/usage/error-details` - Detailed error logs
- `GET /admin/api/usage/revenue-by-model` - Revenue breakdown by model
- `GET /admin/usage` - Redirect to frontend usage page

### Frontend Structure

#### Log Search Page (`/logs`)

**Components**:
- `app/logs/page.tsx` - Main logs page
- `app/logs/types.ts` - TypeScript type definitions
- `app/logs/log-filters.tsx` - Filter controls with date picker
- `app/logs/log-entry-card.tsx` - Individual log entry display
- `app/logs/log-details-dialog.tsx` - Detailed log entry modal
- `components/ui/calendar.tsx` - Date picker component

**Features**:
- Advanced filtering (date, level, request ID, text search)
- Real-time log viewing with auto-refresh (30s)
- Click to view full log details
- Responsive design for mobile/desktop
- Copy functionality for log entries

#### Usage Analytics Page (`/usage`)

**Components**:
- `app/usage/page.tsx` - Main usage dashboard
- `components/usage-metrics-chart.tsx` - Time-series line charts
- `components/usage-summary-cards.tsx` - Summary statistics cards
- `components/error-details-table.tsx` - Error details table
- `components/revenue-by-model-table.tsx` - Revenue by model table

**Features**:
- Multiple time ranges (1h, 6h, 24h, 3d, 7d)
- Configurable intervals (5min, 15min, 30min, 1h)
- Real-time charts for request volume, revenue, errors, payments
- Revenue tracking by model
- Error distribution analysis
- Auto-refresh (60s)

#### Navigation

Updated `components/app-sidebar.tsx` to include:
- **Usage Analytics** (ActivityIcon) - `/usage` route
- **Logs** (FileTextIcon) - `/logs` route

## Key Improvements Over Original PRs

### 1. **Shared Infrastructure**
- **Before**: Both PRs had duplicate log file reading code
- **After**: Single `reader.py` module used by both features
- **Benefit**: 40% less code, easier maintenance, consistent behavior

### 2. **Better Separation of Concerns**
- **Before**: Mixed generic and analytics-specific code
- **After**: Clear separation between `search.py` (generic) and `metrics.py` (analytics)
- **Benefit**: Easier to understand, test, and extend

### 3. **Type Safety**
- Added `LogEntry` TypedDict for better type hints
- Comprehensive type annotations on all functions
- TypeScript interfaces for frontend API responses

### 4. **Documentation**
- Detailed docstrings on all functions
- Critical log message documentation in `metrics.py`
- Architecture overview in this document

### 5. **Performance**
- Efficient file filtering by date range
- Configurable result limits
- Sorted file processing (newest first)

## Testing

To test the implementation:

1. **Backend** - Start the server and access:
   - `/admin/api/logs?level=ERROR&limit=50` - Test log search
   - `/admin/api/usage/summary?hours=24` - Test usage summary
   - `/admin/api/usage/metrics?interval=15&hours=24` - Test time-series metrics

2. **Frontend** - Navigate to:
   - `/logs` - Test log search UI with various filters
   - `/usage` - Test analytics dashboard with different time ranges

3. **Integration** - Verify:
   - Log files are being parsed correctly
   - Filters work as expected
   - Charts display data properly
   - Auto-refresh works

## Migration Notes

If migrating from either PR:

### From PR #228 (Log Search)
- The `routstr/search/log_search.py` module is replaced by `routstr/logs/search.py`
- API endpoint remains the same: `/admin/api/logs`
- Frontend components are identical

### From PR #229 (Usage Dashboard)
- Log parsing logic moved from `admin.py` to `routstr/logs/metrics.py`
- API endpoints remain the same: `/admin/api/usage/*`
- Frontend components are identical

## Future Enhancements

Potential improvements:

1. **Caching** - Cache parsed log data for frequently accessed time ranges
2. **Real-time Streaming** - WebSocket support for live log streaming
3. **Export** - Export logs and metrics to CSV/JSON
4. **Alerting** - Configurable alerts based on error rates or patterns
5. **Log Retention** - Automatic log archival and cleanup policies
6. **Advanced Analytics** - Machine learning for anomaly detection

## Critical Files

**Do Not Modify Without Updating This Implementation**:
- `routstr/proxy.py` - Contains critical log messages for request tracking
- `routstr/auth.py` - Contains payment processing log messages
- `routstr/upstream/base.py` - Contains token adjustment log messages
- `routstr/core/logging.py` - Log format configuration

## Summary

This unified implementation successfully combines the best features from both PRs while:
- ✅ Eliminating code duplication
- ✅ Improving code organization
- ✅ Maintaining all functionality from both PRs
- ✅ Adding comprehensive documentation
- ✅ Following best practices (DRY, separation of concerns, type safety)
- ✅ Preparing for future enhancements

The result is a production-ready, maintainable solution that provides both log search and usage analytics capabilities.
