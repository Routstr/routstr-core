# Testing Guide

This guide covers testing practices, patterns, and tools used in Routstr Core development.

## Testing Philosophy

We follow these principles:

- Test behavior, not implementation
- Fast feedback
- Reliable tests
- Clear failures

## Test Structure

```
tests/
├── integration/
│   ├── conftest.py
│   ├── utils.py
│   ├── test_wallet_topup.py
│   ├── test_wallet_refund.py
│   ├── test_wallet_information.py
│   ├── test_proxy_get_endpoints.py
│   ├── test_proxy_post_endpoints.py
│   └── ... more integration tests
├── unit/
│   ├── test_algorithm.py
│   ├── test_fee_consistency.py
│   ├── test_image_tokens.py
│   ├── test_logging_securityfilter.py
│   ├── test_payment_helpers.py
│   ├── test_settings.py
│   ├── test_wallet.py
│   └── ... more unit tests
└── run_integration.py
```

## Running Tests

### Make Targets

```bash
# Run all tests (unit + integration with mocks)
make test

# Unit tests only
make test-unit

# Integration tests with mocks (fast)
make test-integration

# Integration tests with Docker services
make test-integration-docker

# Fast tests only (skip slow and Docker tests)
make test-fast

# Performance tests
make test-performance

# Coverage
make test-coverage
```

### Direct pytest Commands

```bash
# Run all tests
pytest

# Run a specific test file
pytest tests/unit/test_wallet.py -v

# Run a specific test
pytest tests/unit/test_wallet.py::test_get_balance -v

# Run tests matching a pattern
pytest -k "wallet" -v
```

## Test Modes (Integration)

Integration tests support two execution modes:

- Mock mode (default): uses in-memory mocks, no Docker required
- Docker mode: uses real Docker services (Cashu mint, mock OpenAI, Nostr relay)

Use the runner script for Docker mode:

```bash
./tests/run_integration.py
```

Or manually:

```bash
docker-compose -f compose.testing.yml up -d
USE_LOCAL_SERVICES=1 pytest tests/integration/ -v
docker-compose -f compose.testing.yml down -v
```

## Test Markers

Markers are defined in `pyproject.toml`:

- `integration`
- `unit`
- `slow`
- `requires_docker`
- `requires_real_mint`
- `performance`
- `asyncio`

Examples:

```bash
# Skip slow tests
pytest -m "not slow" -v

# Run only integration tests
pytest -m "integration" -v

# Run performance tests
pytest -m "performance" -v
```

## Fixtures and Utilities

### Core Integration Fixtures

Defined in `tests/integration/conftest.py`:

- `integration_client` - Async HTTP client for the FastAPI app
- `authenticated_client` - Client with a pre-created API key
- `testmint_wallet` - Test wallet for generating Cashu tokens
- `db_snapshot` - Database state snapshot/diff helper
- `create_api_key` - Helper to create API keys for tests
- `integration_engine`, `integration_session` - Async DB engine/session
- `background_tasks_controller` - Control background tasks in tests
- `mock_upstream_server` - Mock upstream API responses

### Integration Utilities

Defined in `tests/integration/utils.py`:

- `CashuTokenGenerator`
- `ResponseValidator`
- `PerformanceValidator`
- `ConcurrencyTester`
- `DatabaseStateValidator`
- `MockServiceBuilder`
- `TestDataBuilder`

## Writing Tests

### Unit Test Example

```python
from routstr.algorithm import calculate_model_cost_score
from routstr.payment.models import Architecture, Model, Pricing


def test_calculate_model_cost_score_basic() -> None:
    model = Model(
        id="test-model",
        name="Test test-model",
        created=1234567890,
        description="Test model",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="gpt",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=0.001,
            completion=0.002,
            request=0.0,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0,
        ),
    )
    assert calculate_model_cost_score(model) == 0.002
```

### Integration Test Example

```python
import pytest
from httpx import AsyncClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_topup(
    authenticated_client: AsyncClient,
    testmint_wallet: object,
    db_snapshot: object,
) -> None:
    await db_snapshot.capture()
    token = await testmint_wallet.mint_tokens(1000)
    response = await authenticated_client.post(
        "/v1/wallet/topup", params={"cashu_token": token}
    )
    assert response.status_code == 200
    diff = await db_snapshot.diff()
    assert len(diff["api_keys"]["modified"]) == 1
```

## Debugging Tips

```bash
# Show print output
pytest -s tests/unit/test_wallet.py

# Drop into debugger on failure
pytest --pdb
```

## Troubleshooting

- Docker mode failures: check `docker ps` and `docker-compose -f compose.testing.yml logs`
- Connection errors: make sure ports 3338, 3000, 8000, and 8088 are free
- Slow tests: use `pytest -m "not slow"` or `make test-fast`

## Next Steps

- See [Architecture](architecture.md)
- Read [Setup Guide](setup.md)
