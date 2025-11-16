# Test Suite Action Plan - Quick Reference

## Immediate Actions (This Week)

### 1. Create Upstream Provider Test Foundation
**Priority:** CRITICAL  
**Effort:** 2 days  
**Files to create:**
- `tests/unit/upstream/__init__.py`
- `tests/unit/upstream/test_base_provider.py`
- `tests/unit/upstream/test_openai_provider.py`
- `tests/unit/upstream/test_anthropic_provider.py`

**Test scenarios:**
```python
# test_openai_provider.py
def test_prepare_headers_with_api_key()
def test_prepare_headers_removes_cashu_headers()
def test_transform_request_body()
def test_handle_streaming_response()
def test_handle_error_response_401()
def test_handle_error_response_429()
def test_model_list_caching()
```

### 2. Add Admin Endpoint Tests  
**Priority:** CRITICAL  
**Effort:** 2 days  
**File to create:**
- `tests/integration/test_admin_endpoints.py`

**Test scenarios:**
```python
def test_admin_list_models()
def test_admin_update_model_pricing()
def test_admin_disable_model()
def test_admin_enable_model()
def test_admin_add_upstream_provider()
def test_admin_update_upstream_provider()
def test_admin_delete_upstream_provider()
def test_admin_unauthorized_access()
def test_admin_update_settings()
```

### 3. Add Cost Calculation Unit Tests
**Priority:** HIGH  
**Effort:** 1 day  
**File to create:**
- `tests/unit/test_cost_calculation.py`

**Test scenarios:**
```python
def test_calculate_cost_with_usage_data()
def test_calculate_cost_without_usage_data()
def test_calculate_cost_fixed_pricing_mode()
def test_calculate_cost_dynamic_pricing_mode()
def test_calculate_cost_invalid_model()
def test_calculate_cost_missing_pricing()
def test_calculate_cost_zero_tokens()
def test_calculate_cost_large_tokens()
def test_calculate_cost_rounding()
```

### 4. Audit Existing Tests for Reserved Balance
**Priority:** HIGH  
**Effort:** 1 day  
**Files to update:**
- `tests/integration/test_wallet_topup.py`
- `tests/integration/test_proxy_post_endpoints.py`
- `tests/integration/test_wallet_authentication.py`

**Changes needed:**
```python
# BEFORE:
assert wallet_response.json()["balance"] == expected_balance

# AFTER:
wallet_data = wallet_response.json()
assert wallet_data["balance"] == expected_available_balance
assert wallet_data["reserved"] == expected_reserved_balance
# Verify total_balance = balance + reserved
```

---

## Week 1-2 Detailed Plan

### Day 1-2: Upstream Provider Tests
1. Create test infrastructure:
   ```bash
   mkdir -p tests/unit/upstream
   touch tests/unit/upstream/__init__.py
   touch tests/unit/upstream/test_base_provider.py
   ```

2. Write base provider tests (20 tests):
   - Test header preparation
   - Test request transformation
   - Test response handling
   - Test error mapping
   - Test streaming support

3. Write OpenAI provider tests (15 tests):
   - Test API key handling
   - Test model name transformation
   - Test response structure validation
   - Test error codes mapping

4. Write Anthropic provider tests (15 tests):
   - Test x-api-key header
   - Test Claude-specific transformations
   - Test streaming format

### Day 3-4: Admin Tests
1. Create admin test file:
   ```bash
   touch tests/integration/test_admin_endpoints.py
   ```

2. Write model management tests (15 tests):
   - CRUD operations
   - Validation tests
   - Permission tests

3. Write provider management tests (15 tests):
   - Add/update/delete providers
   - Fee configuration
   - Provider URL validation

4. Write settings management tests (10 tests):
   - Update settings
   - Validation
   - Database vs env precedence

### Day 5: Cost Calculation Tests
1. Create cost calculation test file:
   ```bash
   touch tests/unit/test_cost_calculation.py
   ```

2. Write comprehensive tests (30 tests):
   - All pricing modes
   - Edge cases
   - Rounding behavior
   - Error scenarios

---

## Week 3-4: Core Functionality

### Proxy Core Tests (40 tests)
**File:** `tests/unit/test_proxy_core.py`

```python
# Model mapping tests
def test_initialize_upstreams_success()
def test_initialize_upstreams_empty_db()
def test_initialize_upstreams_invalid_provider()
def test_refresh_model_maps_with_overrides()
def test_refresh_model_maps_with_disabled_models()
def test_get_model_instance_existing()
def test_get_model_instance_missing()
def test_get_provider_for_model_existing()
def test_get_provider_for_model_missing()

# Routing tests
def test_route_to_cheapest_provider()
def test_route_with_exact_model_match()
def test_route_with_alias_match()
def test_route_with_provider_penalty()
def test_route_with_disabled_model()

# Error handling tests
def test_invalid_model_error_response()
def test_no_provider_error_response()
def test_upstream_failure_handling()
def test_concurrent_model_refresh()
```

### Middleware Tests (20 tests)
**File:** `tests/unit/test_middleware.py`

```python
def test_request_id_generation()
def test_request_id_propagation()
def test_request_logging_structure()
def test_response_logging_structure()
def test_error_logging()
def test_request_timing()
def test_context_variable_lifecycle()
def test_sensitive_data_redaction()
def test_body_size_logging()
def test_header_filtering()
```

### Concurrency Tests (30 tests)
**File:** `tests/integration/test_concurrency.py`

```python
# Payment concurrency
def test_concurrent_payments_same_key()
def test_concurrent_topups_same_key()
def test_concurrent_refunds()
def test_payment_reversal_race_condition()

# Cache concurrency
def test_concurrent_model_map_refresh()
def test_concurrent_provider_cache_access()
def test_concurrent_settings_updates()

# Database concurrency
def test_concurrent_balance_updates()
def test_concurrent_api_key_creation()
def test_atomic_payment_with_concurrent_topup()
```

---

## Week 5-6: Discovery & Advanced Features

### NIP-91 Tests (40 tests)
**File:** `tests/unit/test_nip91.py`

```python
# Event creation tests
def test_create_nip91_event_basic()
def test_create_nip91_event_with_mints()
def test_create_nip91_event_signing()

# Parsing tests
def test_parse_valid_nip91_event()
def test_parse_invalid_nip91_event()
def test_events_semantically_equal()

# Relay interaction tests (mocked)
def test_query_nip91_events_success()
def test_query_nip91_events_timeout()
def test_publish_to_relay_success()
def test_publish_to_relay_failure()
def test_publish_with_backoff()

# Discovery tests
def test_discover_onion_url_from_tor()
def test_discover_onion_url_not_found()
def test_determine_provider_id()
```

### Discovery Tests (30 tests)
**File:** `tests/unit/test_discovery.py`

```python
# Provider parsing
def test_parse_provider_announcement_nip91()
def test_parse_provider_announcement_invalid()
def test_parse_provider_announcement_missing_fields()

# Cache management
def test_refresh_providers_cache()
def test_providers_cache_concurrent_access()
def test_providers_cache_expiry()

# Health checks
def test_fetch_provider_health_onion()
def test_fetch_provider_health_clearnet()
def test_fetch_provider_health_timeout()
def test_fetch_provider_health_with_tor_proxy()
```

---

## Test Quality Improvements

### Add Test Markers
**File:** `pyproject.toml` or `pytest.ini`

```ini
[tool.pytest.ini_options]
markers =
    unit: Unit tests
    integration: Integration tests
    e2e: End-to-end tests
    slow: Slow-running tests
    concurrency: Concurrency tests
    security: Security-related tests
    wallet: Wallet component tests
    proxy: Proxy component tests
    auth: Authentication tests
    admin: Admin functionality tests
    discovery: Provider discovery tests
    payment: Payment/billing tests
```

### Improve Test Fixtures
**File:** `tests/conftest.py`

```python
# Split TestmintWallet into smaller fixtures
@pytest.fixture
def mint_url():
    """Get configured mint URL"""
    return os.environ.get("MINT", "http://localhost:3338")

@pytest.fixture
def cashu_token_generator(mint_url):
    """Factory for generating cashu tokens"""
    def _generate(amount: int) -> str:
        # Simplified token generation
        ...
    return _generate

@pytest.fixture
def mock_upstream_provider():
    """Factory for creating mock upstream providers"""
    def _create(provider_type: str, **kwargs) -> Mock:
        # Create mock provider with sensible defaults
        ...
    return _create
```

### Add Test Utilities
**File:** `tests/utils/factories.py`

```python
"""Test data factories"""
from factory import Factory, Faker
from factory.alchemy import SQLAlchemyModelFactory

class ApiKeyFactory(SQLAlchemyModelFactory):
    class Meta:
        model = ApiKey
        sqlalchemy_session = None
    
    hashed_key = Faker('sha256')
    balance = 10000000
    reserved_balance = 0
    total_spent = 0
    total_requests = 0

class ModelRowFactory(SQLAlchemyModelFactory):
    class Meta:
        model = ModelRow
        sqlalchemy_session = None
    
    id = Faker('uuid4')
    name = Faker('word')
    enabled = True
    pricing = '{"prompt": 0.001, "completion": 0.002}'
```

---

## Metrics & Monitoring

### Coverage Targets
```bash
# Add to CI pipeline
pytest --cov=routstr --cov-report=html --cov-report=term --cov-fail-under=75

# Per-module targets
routstr/upstream/: 70%
routstr/core/admin.py: 80%
routstr/payment/cost_caculation.py: 80%
routstr/proxy.py: 75%
routstr/auth.py: 80%
```

### Performance Monitoring
```python
# Add to tests
@pytest.mark.benchmark
def test_model_mapping_performance(benchmark):
    result = benchmark(refresh_model_maps)
    assert result < 0.1  # Should complete in <100ms
```

### Test Execution Time
```bash
# Current target: All tests < 5 minutes
# Unit tests: < 30 seconds
# Integration tests: < 4 minutes
```

---

## Tools & Dependencies

### Add to requirements.txt
```
pytest-benchmark==4.0.0  # Performance testing
pytest-xdist==3.3.1  # Parallel test execution
factory-boy==3.3.0  # Test data factories
hypothesis==6.92.0  # Property-based testing
mutmut==2.4.4  # Mutation testing
pytest-timeout==2.2.0  # Prevent hanging tests
pytest-asyncio==0.23.0  # Already present, ensure latest
```

### Configure pytest-xdist
```bash
# Run tests in parallel
pytest -n auto

# Or specify workers
pytest -n 4
```

---

## Weekly Review Checklist

### End of Week 1
- [ ] Upstream provider tests created (50+ tests)
- [ ] Admin endpoint tests created (40+ tests)
- [ ] Cost calculation tests created (30+ tests)
- [ ] Existing tests audited for reserved_balance
- [ ] Coverage increased from 40% to ~55%

### End of Week 2
- [ ] All CRITICAL tests completed
- [ ] CI pipeline updated with coverage checks
- [ ] Test documentation improved
- [ ] Coverage increased from 55% to ~65%

### End of Week 4
- [ ] Proxy core tests completed (40 tests)
- [ ] Middleware tests completed (20 tests)
- [ ] Concurrency tests completed (30 tests)
- [ ] Coverage increased from 65% to ~70%

### End of Week 6
- [ ] NIP-91 tests completed (40 tests)
- [ ] Discovery tests completed (30 tests)
- [ ] Property-based tests added (10 tests)
- [ ] Coverage target achieved: 75%+
- [ ] All high-priority gaps closed

---

## Success Criteria

### Code Quality
- [ ] All tests pass consistently
- [ ] No flaky tests
- [ ] Test execution time < 5 minutes
- [ ] Coverage > 75% overall
- [ ] All CRITICAL components > 70% coverage

### Documentation
- [ ] All test modules have docstrings
- [ ] Test scenarios are clearly documented
- [ ] Test data is well-documented
- [ ] README updated with testing guide

### Automation
- [ ] Coverage reports in CI
- [ ] Automated test quality checks
- [ ] Performance regression detection
- [ ] Test result trends tracked

---

## Getting Help

### Questions?
- Review full analysis: `TEST_SUITE_ANALYSIS_REPORT.md`
- Check existing tests for patterns
- Ask team for test data sources
- Review pytest documentation

### Blocked?
- Can't mock something? Consider using real service
- Test too slow? Use pytest-xdist
- Test too complex? Split into smaller tests
- Coverage not improving? Use coverage report to find gaps

---

**Last Updated:** 2025-11-16  
**Next Review:** End of Week 1
