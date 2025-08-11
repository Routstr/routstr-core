# FastAPI Async Unit Tests

This directory contains async unit tests for the Routstr proxy FastAPI application.

## Installation

First, ensure you have the development dependencies installed:

```bash
uv pip install -e ".[dev]"
```

## Running Tests

To run all tests:

```bash
pytest
```

To run tests with coverage:

```bash
pytest --cov=router --cov-report=html
```

To run specific test files:

```bash
pytest tests/test_main.py
pytest tests/test_models.py
pytest tests/test_proxy.py
```

To run only async tests:

```bash
pytest -m asyncio
```

## Test Structure

- `conftest.py` - Pytest fixtures and configuration
- `test_main.py` - Tests for main app endpoints
- `test_account.py` - Tests for wallet/account management endpoints
- `test_proxy.py` - Tests for the proxy functionality with mocked upstream
- `test_models.py` - Tests for model pricing and data structures

## Key Fixtures

- `async_client` - Async HTTP client for testing FastAPI endpoints
- `test_session` - In-memory SQLite database session for tests
- `test_api_key` - Pre-configured API key with balance
- `api_key_with_balance` - API key with sufficient balance for proxy tests

## Environment Variables

The tests automatically set up required environment variables in `conftest.py`. No manual configuration needed.

## Writing New Tests

1. Use `@pytest.mark.asyncio` for async tests
2. Use the provided fixtures for database and client access
3. Mock external dependencies (like upstream API calls)
4. Test both success and error cases
5. Verify database state changes when applicable
