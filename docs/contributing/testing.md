# Testing Guide

This guide covers testing practices, patterns, and tools used in Routstr Core development.

## Testing Philosophy

We follow these principles:

- **Test Behavior, Not Implementation** - Tests should survive refactoring
- **Fast Feedback** - Unit tests run in milliseconds
- **Reliable Tests** - No flaky tests allowed
- **Clear Failures** - Tests should clearly indicate what broke

## Test Structure

```
tests/
├── __init__.py
├── conftest.py              # Shared fixtures and configuration
├── unit/                    # Fast, isolated unit tests
│   ├── test_auth.py
│   ├── test_balance.py
│   ├── test_cost_calculation.py
│   ├── test_models.py
│   └── test_wallet.py
└── integration/             # Component integration tests
    ├── test_api_endpoints.py
    ├── test_payment_flow.py
    ├── test_proxy_streaming.py
    └── test_real_mint.py
```

## Running Tests

### Quick Test Commands

```bash
# Run all tests
make test

# Run unit tests only (fast)
make test-unit

# Run integration tests
make test-integration

# Run with coverage
make test-coverage

# Run specific test file
uv run pytest tests/unit/test_auth.py -v

# Run specific test
uv run pytest tests/unit/test_auth.py::test_create_api_key -v

# Run tests matching pattern
uv run pytest -k "balance" -v
```

### Test Markers

Use markers to categorize tests:

```python
@pytest.mark.slow
async def test_heavy_computation():
    pass

@pytest.mark.requires_docker
async def test_real_services():
    pass

# Run without slow tests
pytest -m "not slow"

# Run only integration tests
pytest -m "integration"
```

## Writing Tests

### Unit Test Example

```python
# tests/unit/test_auth.py
import pytest
from routstr.auth import create_api_key, validate_api_key

class TestAPIKeyAuth:
    """Test API key authentication functionality"""
    
    async def test_create_api_key(self, test_db):
        """Test creating a new API key"""
        # Arrange
        initial_balance = 10000
        
        # Act
        api_key = await create_api_key(
            balance=initial_balance,
            name="Test Key"
        )
        
        # Assert
        assert api_key.key.startswith("sk-")
        assert len(api_key.key) == 32
        assert api_key.balance == initial_balance
        
    async def test_validate_invalid_key(self, test_db):
        """Test validation fails for invalid key"""
        # Act & Assert
        with pytest.raises(InvalidAPIKeyError):
            await validate_api_key("invalid_key")
```

### Integration Test Example

```python
# tests/integration/test_payment_flow.py
import pytest
from httpx import AsyncClient

class TestPaymentFlow:
    """Test end-to-end payment flows"""
    
    @pytest.mark.asyncio
    async def test_token_redemption_flow(
        self, 
        app_client: AsyncClient,
        mock_cashu_wallet
    ):
        """Test complete token redemption and API usage"""
        # Arrange
        token = create_test_token(amount=5000)
        mock_cashu_wallet.redeem.return_value = 5000
        
        # Act - Create API key
        response = await app_client.post(
            "/v1/wallet/create",
            json={"cashu_token": token}
        )
        
        # Assert - Key created
        assert response.status_code == 200
        api_key = response.json()["api_key"]
        assert response.json()["balance"] == 5000
        
        # Act - Use API key
        response = await app_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Hi"}]
            }
        )
        
        # Assert - Request successful
        assert response.status_code == 200
        assert "choices" in response.json()
```

## Test Fixtures

### Common Fixtures

Located in `tests/conftest.py`:

```python
@pytest.fixture
async def test_db():
    """Provide a clean test database"""
    # Create in-memory SQLite database
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    
    async_session = sessionmaker(engine, class_=AsyncSession)
    
    async with async_session() as session:
        yield session
    
    await engine.dispose()

@pytest.fixture
async def app_client(test_db):
    """Provide test client with test database"""
    app.dependency_overrides[get_db] = lambda: test_db
    
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client
    
    app.dependency_overrides.clear()

@pytest.fixture
def mock_cashu_wallet(mocker):
    """Mock Cashu wallet for testing"""
    mock = mocker.patch("routstr.wallet.Wallet")
    mock.return_value.redeem.return_value = 1000
    return mock
```

### Using Fixtures

```python
async def test_with_fixtures(
    test_db,          # Get test database
    app_client,       # Get test HTTP client
    mock_cashu_wallet # Get mocked wallet
):
    # Use fixtures in test
    pass
```

## Mocking Strategies

### Mocking External Services

```python
# Mock upstream API
@pytest.fixture
def mock_openai(mocker):
    mock_response = mocker.Mock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {"content": "Hello!"},
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30
        }
    }
    
    mocker.patch(
        "httpx.AsyncClient.post",
        return_value=mock_response
    )

# Mock Cashu mint
@pytest.fixture
def mock_mint(mocker):
    mint = mocker.patch("routstr.wallet.Mint")
    mint.return_value.check_proof_state.return_value = True
    return mint
```

### Mocking Time

```python
from freezegun import freeze_time

@freeze_time("2024-01-01 12:00:00")
async def test_time_dependent():
    # Time is frozen during test
    key = await create_api_key(expires_in_days=30)
    assert key.expires_at == datetime(2024, 1, 31, 12, 0, 0)
```

## Test Patterns

### Testing Async Code

```python
# Always mark async tests
@pytest.mark.asyncio
async def test_async_function():
    result = await async_operation()
    assert result == expected

# Test async context managers
async def test_async_context():
    async with create_resource() as resource:
        assert resource.is_active
```

### Testing Exceptions

```python
# Test specific exception
async def test_raises_specific_error():
    with pytest.raises(InsufficientBalanceError) as exc_info:
        await deduct_balance(api_key, amount=999999)
    
    assert "Insufficient balance" in str(exc_info.value)
    assert exc_info.value.status_code == 402

# Test exception details
async def test_exception_details():
    with pytest.raises(RoustrError) as exc_info:
        await risky_operation()
    
    error = exc_info.value
    assert error.error_type == "validation_error"
    assert error.detail == "Invalid input"
```

### Testing Streaming Responses

```python
async def test_streaming_response():
    """Test streaming chat completion"""
    chunks = []
    
    async with app_client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "gpt-3.5-turbo", "stream": True}
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                chunks.append(json.loads(line[6:]))
    
    # Verify chunks
    assert len(chunks) > 0
    assert chunks[-1] == "[DONE]"
```

### Database Testing

```python
async def test_database_transaction(test_db):
    """Test atomic transactions"""
    async with test_db.begin():
        # Create test data
        api_key = APIKey(key_hash="test", balance=1000)
        test_db.add(api_key)
        await test_db.flush()
        
        # Test rollback
        try:
            async with test_db.begin_nested():
                api_key.balance = -100  # Invalid
                await test_db.flush()
                raise ValueError("Rollback")
        except ValueError:
            pass
        
        # Verify rollback worked
        await test_db.refresh(api_key)
        assert api_key.balance == 1000
```

## Performance Testing

### Benchmark Tests

```python
@pytest.mark.benchmark
def test_performance(benchmark):
    """Benchmark critical functions"""
    result = benchmark(expensive_function, arg1, arg2)
    assert result == expected

# Run benchmarks
pytest --benchmark-only
```

### Load Testing

```python
# tests/integration/test_load.py
async def test_concurrent_requests(app_client):
    """Test handling multiple concurrent requests"""
    async def make_request(i):
        response = await app_client.get(f"/test/{i}")
        return response.status_code
    
    # Make 100 concurrent requests
    tasks = [make_request(i) for i in range(100)]
    results = await asyncio.gather(*tasks)
    
    # All should succeed
    assert all(status == 200 for status in results)
```

## Test Data

### Factories

```python
# tests/factories.py
from datetime import datetime, timedelta

def create_test_api_key(**kwargs):
    """Factory for test API keys"""
    defaults = {
        "key": f"sk-test-{uuid4().hex[:8]}",
        "balance": 10000,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(days=30)
    }
    defaults.update(kwargs)
    return APIKey(**defaults)

def create_test_token(amount: int = 1000, mint: str = None):
    """Factory for test Cashu tokens"""
    # Create valid test token structure
    return base64.encode(...)
```

### Test Constants

```python
# tests/constants.py
TEST_MODELS = {
    "gpt-3.5-turbo": {
        "prompt": 0.0015,
        "completion": 0.002
    },
    "gpt-4": {
        "prompt": 0.03,
        "completion": 0.06
    }
}

TEST_API_KEY = "sk-test-1234567890"
TEST_MINT_URL = "https://testmint.example.com"
```

## Coverage

### Running Coverage

```bash
# Generate coverage report
make test-coverage

# View HTML report
open htmlcov/index.html

# Coverage with specific tests
pytest --cov=routstr --cov-report=html tests/unit/
```

### Coverage Configuration

In `pyproject.toml`:

```toml
[tool.coverage.run]
source = ["routstr"]
omit = ["tests/*", "*/migrations/*"]

[tool.coverage.report]
exclude_lines = [
    "pragma: no cover",
    "def __repr__",
    "raise AssertionError",
    "raise NotImplementedError",
    "if TYPE_CHECKING:"
]
```

## Debugging Tests

### Print Debugging

```python
# Use -s flag to see print output
pytest -s tests/unit/test_auth.py

# In test
async def test_debug():
    print(f"Value: {value}")  # Will show with -s
    assert value == expected
```

### Interactive Debugging

```python
# Drop into debugger on failure
pytest --pdb

# Set breakpoint in test
async def test_debug():
    import pdb; pdb.set_trace()
    # Execution stops here
```

### Logging in Tests

```python
# Enable debug logging in tests
import logging
logging.basicConfig(level=logging.DEBUG)

# Or use caplog fixture
async def test_logging(caplog):
    with caplog.at_level(logging.INFO):
        await function_that_logs()
    
    assert "Expected message" in caplog.text
```

## CI/CD Integration

### GitHub Actions

Tests run automatically on:

- Pull requests
- Pushes to main
- Nightly schedules

See `.github/workflows/test.yml` for configuration.

### Pre-commit Hooks

Install pre-commit hooks:

```bash
pre-commit install

# Run manually
pre-commit run --all-files
```

## Best Practices

### Do's

1. ✅ Write tests first (TDD)
2. ✅ Keep tests simple and focused
3. ✅ Use descriptive test names
4. ✅ Test edge cases
5. ✅ Mock external dependencies
6. ✅ Use fixtures for setup
7. ✅ Assert specific values

### Don'ts

1. ❌ Don't test implementation details
2. ❌ Don't use production services
3. ❌ Don't rely on test order
4. ❌ Don't ignore flaky tests
5. ❌ Don't skip error cases
6. ❌ Don't use hard-coded waits
7. ❌ Don't commit commented tests

## Troubleshooting

### Common Issues

**Async Test Errors**

```python
# Wrong
def test_async():  # Missing async
    await function()

# Right
async def test_async():
    await function()
```

**Database State**

```python
# Ensure clean state
@pytest.fixture(autouse=True)
async def cleanup(test_db):
    yield
    # Cleanup after each test
    await test_db.execute("DELETE FROM apikey")
    await test_db.commit()
```

**Mock Not Working**

```python
# Check import path
mocker.patch("routstr.wallet.Wallet")  # Full path
# Not just "Wallet"
```

## Next Steps

- See [Architecture](architecture.md) for system design
- Read [Setup Guide](setup.md) for environment setup
