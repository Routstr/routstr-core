# Integration Tests

End-to-end tests for API endpoints, Cashu wallet operations, and database interactions.

## Quick Start

```bash
# First-time setup (installs uv if needed)
make setup

# Check if all dependencies are installed
make check-deps

# Run tests
make test
```

## Test Modes

The integration tests support two execution modes:

### 🎭 Mock Mode (Default - Fast)

- Uses in-memory mocks for external services
- No Docker required
- Runs quickly, ideal for CI/CD
- Good for rapid development iteration

### 🐳 Docker Mode (Realistic)

- Uses real Docker services (Cashu mint, mock OpenAI, Nostr relay)
- More accurate testing environment
- Slower but catches more edge cases
- Recommended before releases

## Running Tests

### Quick Mode (Mocked Services)

```bash
# All integration tests with mocks
pytest tests/integration/ -v

# Specific test file
pytest tests/integration/test_wallet_topup.py -v

# Skip slow tests
pytest tests/integration/ -m "not slow" -v

# Run only unit-style integration tests
pytest tests/integration/ -m "not requires_docker" -v
```

### Full Integration Mode (Docker Services)

```bash
# Using the automated script (recommended)
./tests/run_integration.py

# Or manually:
docker-compose -f compose.testing.yml up -d
USE_LOCAL_SERVICES=1 pytest tests/integration/ -v
docker-compose -f compose.testing.yml down -v
```

### PostgreSQL Mode

`test_postgres_compatibility.py` runs the main application DB against a real
PostgreSQL server. It covers the full Alembic chain in both directions plus the
billing, reservation, refund, invoice and payout paths, and it catches the
things SQLite cannot express: native enum types, INT4 column widths, per-dialect
`ON CONFLICT`, and genuinely concurrent transactions.

Every test there skips unless `ROUTSTR_TEST_POSTGRES_URL` is set, so the default
suite is unaffected.

```bash
docker run -d --name routstr-pg \
  -e POSTGRES_PASSWORD=routstr -e POSTGRES_USER=routstr -e POSTGRES_DB=routstr \
  -p 55433:5432 postgres:16-alpine

ROUTSTR_TEST_POSTGRES_URL=postgresql+asyncpg://routstr:routstr@127.0.0.1:55433/routstr \
  pytest tests/integration/test_postgres_compatibility.py -v

docker rm -f routstr-pg
```

Each test drops and recreates the `public` schema, so point it only at a
disposable database.

The dialect-portability invariants that do **not** need a server — column
widths, enum rendering, per-dialect upsert selection — live in
`tests/unit/test_postgres_schema_compat.py` and run in the normal suite.

### CI/CD Mode

```bash
# Fast tests only for continuous integration
pytest tests/integration/ -m "not slow and not requires_docker" -v

# Performance tests
pytest tests/integration/ -m "performance" -v
```

## Test Infrastructure

### Core Fixtures

- **`integration_client`** - Async HTTP client configured for testing
- **`authenticated_client`** - Pre-authenticated client with API key
- **`testmint_wallet`** - Mock/real Cashu wallet for token generation
- **`db_snapshot`** - Database state tracking for verification
- **`test_mode`** - Reports current execution mode (mock/docker)

### Utility Classes

- **`ResponseValidator`** - Validates API response formats
- **`PerformanceValidator`** - Tracks and validates performance metrics
- **`ConcurrencyTester`** - Tests concurrent request handling
- **`CashuTokenGenerator`** - Generates valid/invalid test tokens

## Environment Configuration

Test environment configuration is handled directly in `conftest.py`. The configuration automatically switches between:

- **Mock mode**: Fast, uses mocked services (default)
- **Docker mode**: Uses real Docker services when `USE_LOCAL_SERVICES=1`

This keeps all test configuration in one place and avoids file duplication.

## Writing Tests

### Basic Test Structure

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_topup(
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
    db_snapshot: Any
):
    # Capture initial state
    await db_snapshot.capture()
    
    # Generate test token
    token = await testmint_wallet.mint_tokens(1000)
    
    # Make API request
    response = await authenticated_client.post(
        "/v1/wallet/topup", 
        params={"cashu_token": token}
    )
    
    # Validate response
    assert response.status_code == 200
    
    # Verify database changes
    diff = await db_snapshot.diff()
    assert len(diff["api_keys"]["modified"]) == 1
```

### Testing Concurrent Operations

```python
async def test_concurrent_topups(
    integration_client: AsyncClient,
    testmint_wallet: Any,
    create_api_key: Callable
):
    # Create multiple API keys
    keys = []
    for i in range(5):
        key, _ = await create_api_key(integration_client, testmint_wallet)
        keys.append(key)
    
    # Test concurrent requests
    tester = ConcurrencyTester()
    responses = await tester.run_concurrent_requests(
        integration_client,
        [{"method": "GET", "url": "/v1/wallet/", 
          "headers": {"Authorization": f"Bearer {key}"}} 
         for key in keys],
        max_concurrent=5
    )
    
    # All should succeed
    assert all(r.status_code == 200 for r in responses)
```

### Performance Testing

```python
@pytest.mark.performance
async def test_endpoint_performance(
    authenticated_client: AsyncClient,
    performance_validator: PerformanceValidator
):
    # Run multiple requests
    for i in range(100):
        start = performance_validator.start_timing("wallet_info")
        response = await authenticated_client.get("/v1/wallet/")
        performance_validator.end_timing("wallet_info", start)
    
    # Validate 95th percentile < 100ms
    result = performance_validator.validate_response_time(
        "wallet_info", max_duration=0.1, percentile=0.95
    )
    assert result["valid"], f"P95: {result['percentile_time']:.3f}s"
```

## Troubleshooting

### Tests Failing with Connection Errors

- Ensure Docker services are running: `docker ps`
- Check service logs: `docker-compose -f compose.testing.yml logs`
- Verify ports aren't in use: `lsof -i :3338,3000,8000,8088`

### Mock vs Docker Mode Confusion

- Check current mode: Look for 🎭 or 🐳 emoji in test output
- Force mock mode: Unset `USE_LOCAL_SERVICES`
- Force Docker mode: `export USE_LOCAL_SERVICES=1`

### Slow Test Execution

- Use mock mode for development: `pytest tests/integration/`
- Skip slow tests: `pytest -m "not slow"`
- Run specific test files only
- Use pytest-xdist for parallel execution: `pytest -n auto`

### Installing uv Manually

If `make dev-setup` fails to install uv automatically:

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or with pip
pip install uv

# Or with Homebrew
brew install uv
```

## Best Practices

1. **Use Mock Mode for Development** - It's fast and catches most issues
2. **Run Docker Mode Before PRs** - Ensures realistic testing
3. **Add Appropriate Markers** - Help others run relevant test subsets
   - Use `@pytest.mark.slow` for tests that take significant time (e.g., memory/load tests)
   - Use `@pytest.mark.requires_docker` for tests needing Docker services
4. **Verify Database State** - Use `db_snapshot` for state verification
5. **Test Edge Cases** - Invalid inputs, network failures, race conditions
6. **Monitor Performance** - Add performance tests for critical paths
