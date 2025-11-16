# Test Coverage Matrix

Quick visual reference for test coverage across the Routstr codebase.

## Legend
- ✅ **Excellent:** >80% coverage, comprehensive tests
- ✔️ **Good:** 60-80% coverage, most scenarios covered
- ⚠️ **Partial:** 30-60% coverage, major gaps exist
- ❌ **Critical Gap:** <30% coverage or no tests
- 🔴 **High Risk:** Untested critical functionality

---

## Core Components

| Component | File | Unit Tests | Integration Tests | Status | Risk |
|-----------|------|------------|-------------------|--------|------|
| **Proxy Router** | `proxy.py` | ❌ None | ⚠️ Indirect | ❌ | 🔴 |
| Algorithm | `algorithm.py` | ✅ Excellent | ⚠️ Indirect | ✅ | ✅ |
| Authentication | `auth.py` | ⚠️ Partial | ✔️ Good | ⚠️ | ⚠️ |
| Balance | `balance.py` | ❌ None | ✔️ Good | ⚠️ | ⚠️ |
| Discovery | `discovery.py` | ❌ None | ⚠️ Mocked | ❌ | ⚠️ |
| NIP-91 | `nip91.py` | ❌ None | ❌ None | ❌ | 🔴 |

## Core Modules

| Module | File | Unit Tests | Integration Tests | Status | Risk |
|--------|------|------------|-------------------|--------|------|
| **Admin** | `core/admin.py` | ❌ None | ❌ None | ❌ | 🔴 |
| Database | `core/db.py` | ⚠️ Partial | ✔️ Good | ✔️ | ✅ |
| Settings | `core/settings.py` | ⚠️ Minimal | ⚠️ Basic | ⚠️ | ⚠️ |
| Logging | `core/logging.py` | ❌ None | ❌ None | ❌ | ⚠️ |
| **Middleware** | `core/middleware.py` | ❌ None | ❌ None | ❌ | 🔴 |
| Main App | `core/main.py` | ❌ None | ✔️ Indirect | ⚠️ | ✅ |
| Exceptions | `core/exceptions.py` | ❌ None | ⚠️ Indirect | ⚠️ | ✅ |

## Payment System

| Component | File | Unit Tests | Integration Tests | Status | Risk |
|-----------|------|------------|-------------------|--------|------|
| **Cost Calculation** | `payment/cost_caculation.py` | ❌ None | ⚠️ Indirect | ❌ | 🔴 |
| Payment Helpers | `payment/helpers.py` | ✔️ Partial | ⚠️ Indirect | ⚠️ | ⚠️ |
| Models | `payment/models.py` | ✔️ Good | ⚠️ Indirect | ✔️ | ✅ |
| Price | `payment/price.py` | ❌ None | ⚠️ Mocked | ⚠️ | ⚠️ |
| LNURL | `payment/lnurl.py` | ❌ None | ⚠️ Indirect | ⚠️ | ⚠️ |

## Upstream Providers

| Provider | File | Unit Tests | Integration Tests | Status | Risk |
|----------|------|------------|-------------------|--------|------|
| **Base Provider** | `upstream/base.py` | ❌ None | ⚠️ Indirect | ❌ | 🔴 |
| **OpenAI** | `upstream/openai.py` | ❌ None | ⚠️ Mocked | ❌ | 🔴 |
| **Anthropic** | `upstream/anthropic.py` | ❌ None | ⚠️ Mocked | ❌ | 🔴 |
| **Azure** | `upstream/azure.py` | ❌ None | ⚠️ Mocked | ❌ | 🔴 |
| **Groq** | `upstream/groq.py` | ❌ None | ⚠️ Mocked | ❌ | 🔴 |
| **Perplexity** | `upstream/perplexity.py` | ❌ None | ⚠️ Mocked | ❌ | 🔴 |
| **XAI** | `upstream/xai.py` | ❌ None | ⚠️ Mocked | ❌ | 🔴 |
| **Fireworks** | `upstream/fireworks.py` | ❌ None | ⚠️ Mocked | ❌ | 🔴 |
| **OpenRouter** | `upstream/openrouter.py` | ❌ None | ⚠️ Mocked | ❌ | 🔴 |
| **Ollama** | `upstream/ollama.py` | ❌ None | ⚠️ Mocked | ❌ | 🔴 |
| **Generic** | `upstream/generic.py` | ❌ None | ⚠️ Mocked | ❌ | 🔴 |
| Helpers | `upstream/helpers.py` | ❌ None | ⚠️ Indirect | ❌ | ⚠️ |

## Wallet & Authentication

| Component | Functionality | Unit Tests | Integration Tests | Status | Risk |
|-----------|--------------|------------|-------------------|--------|------|
| Wallet Core | `wallet.py` | ✔️ Good | ✔️ Good | ✔️ | ✅ |
| API Key Gen | Authentication | ⚠️ Partial | ✅ Excellent | ✔️ | ✅ |
| Topup | Wallet balance | ⚠️ Partial | ✅ Excellent | ✔️ | ✅ |
| Refund | Wallet balance | ⚠️ Partial | ✅ Excellent | ✔️ | ✅ |
| Token Validation | Authentication | ⚠️ Partial | ✔️ Good | ⚠️ | ⚠️ |
| Payment Flow | Balance deduction | ⚠️ Partial | ✔️ Good | ⚠️ | ⚠️ |
| Cashu Integration | Token handling | ✔️ Mocked | ⚠️ Fallback | ⚠️ | ⚠️ |

---

## Functionality Coverage

### Authentication & Authorization
| Function | Unit | Integration | Notes |
|----------|------|-------------|-------|
| `validate_bearer_key()` | ❌ | ✔️ | Only integration tested |
| Bearer token auth | ❌ | ✅ | Well covered in integration |
| Cashu token auth | ⚠️ | ✔️ | Mocked in unit, fallback in integration |
| API key creation | ❌ | ✅ | Excellent integration coverage |
| Key expiry handling | ❌ | ⚠️ | Limited coverage |
| Refund address | ❌ | ⚠️ | Partial coverage |

### Payment & Billing
| Function | Unit | Integration | Notes |
|----------|------|-------------|-------|
| `pay_for_request()` | ❌ | ⚠️ | Indirect testing only |
| `revert_pay_for_request()` | ❌ | ⚠️ | Limited coverage |
| `adjust_payment_for_tokens()` | ❌ | ⚠️ | Complex logic not unit tested |
| `calculate_cost()` | ❌ | ⚠️ | No direct tests |
| `get_max_cost_for_model()` | ✔️ | ⚠️ | Basic unit tests |
| Reserved balance handling | ❌ | ⚠️ | New feature, needs tests |
| Concurrent payments | ❌ | ❌ | Critical gap |

### Routing & Proxy
| Function | Unit | Integration | Notes |
|----------|------|-------------|-------|
| `initialize_upstreams()` | ❌ | ⚠️ | Indirect only |
| `refresh_model_maps()` | ❌ | ⚠️ | Background task not tested |
| `create_model_mappings()` | ✅ | ❌ | Good unit coverage |
| `should_prefer_model()` | ✅ | ❌ | Good unit coverage |
| `proxy()` handler | ❌ | ⚠️ | Main proxy logic not unit tested |
| Model resolution | ✔️ | ⚠️ | Algorithm tested, routing not |
| Provider selection | ✔️ | ⚠️ | Algorithm tested, integration partial |

### Discovery & NIP-91
| Function | Unit | Integration | Notes |
|----------|------|-------------|-------|
| `query_nostr_relay_for_providers()` | ❌ | ⚠️ | Mocked in integration |
| `parse_provider_announcement()` | ❌ | ⚠️ | Indirect coverage |
| `create_nip91_event()` | ❌ | ❌ | No tests |
| `publish_to_relay()` | ❌ | ❌ | No tests |
| `announce_provider()` | ❌ | ❌ | Background task not tested |
| `fetch_provider_health()` | ❌ | ⚠️ | Mocked in integration |

### Admin & Management
| Endpoint | Unit | Integration | Notes |
|----------|------|-------------|-------|
| List models | ❌ | ❌ | No coverage |
| Update model | ❌ | ❌ | No coverage |
| Disable model | ❌ | ❌ | No coverage |
| List providers | ❌ | ❌ | No coverage |
| Add provider | ❌ | ❌ | No coverage |
| Update provider | ❌ | ❌ | No coverage |
| Delete provider | ❌ | ❌ | No coverage |
| Update settings | ❌ | ❌ | No coverage |

---

## Test Quality Metrics

### Unit Tests
| Metric | Current | Target | Gap |
|--------|---------|--------|-----|
| Total unit tests | ~200 | ~450 | +250 tests |
| Avg assertions/test | 2.5 | 3.5 | +1 |
| Avg lines/test | 15 | 20 | +5 |
| Fixtures used | 15 | 30 | +15 |
| Mocking quality | Fair | Good | Improve |

### Integration Tests
| Metric | Current | Target | Gap |
|--------|---------|--------|-----|
| Total integration tests | ~100 | ~150 | +50 tests |
| Real services used | Few | More | Improve |
| Mock overuse | High | Medium | Reduce |
| E2E scenarios | 0 | 10 | +10 tests |

### Coverage
| Area | Current | Target | Priority |
|------|---------|--------|----------|
| **Overall** | **40%** | **75%** | **High** |
| Wallet | 85% | 90% | Low |
| Authentication | 65% | 85% | Medium |
| Proxy/Routing | 35% | 80% | **Critical** |
| Upstream Providers | 20% | 65% | **Critical** |
| Payment/Cost | 50% | 75% | High |
| Discovery | 30% | 70% | Medium |
| Admin | 0% | 80% | **Critical** |
| NIP-91 | 0% | 60% | High |
| Middleware | 0% | 75% | **Critical** |

---

## Critical Path Testing Status

### Request Flow (Authenticated)
```
Request → Middleware → Auth → Routing → Upstream → Response → Payment
   ❌         ❌         ⚠️      ❌         ❌          ⚠️        ⚠️
```

### Request Flow (Cashu)
```
Request → Middleware → Cashu Validate → Routing → Upstream → Response → Payment
   ❌         ❌            ⚠️              ❌         ❌          ⚠️        ⚠️
```

### Wallet Operations
```
Create Key → Validate → Topup → Pay → Refund
    ✅          ⚠️       ✅     ⚠️     ✅
```

### Model Selection
```
Request → Parse Model → Find Provider → Check Pricing → Route
   ⚠️         ⚠️            ❌              ❌           ❌
```

---

## Risk Heat Map

### 🔴 Critical Risk (Test Immediately)
1. **Upstream Provider Logic** - 0% unit coverage, 11 files
2. **Admin Endpoints** - 0% coverage, critical functionality
3. **Proxy Core Routing** - No unit tests for main handler
4. **Middleware** - No tests for logging/tracking
5. **Cost Calculation** - No direct tests for billing logic

### ⚠️ High Risk (Test This Sprint)
6. **Payment Atomicity** - Race conditions not tested
7. **NIP-91 Discovery** - Background tasks not tested
8. **Reserved Balance** - New feature needs thorough testing
9. **Concurrent Operations** - Limited concurrency testing
10. **Error Handling** - Edge cases not systematically tested

### ✅ Low Risk (Maintain Coverage)
11. Wallet operations - Good coverage
12. Algorithm logic - Well tested
13. API key generation - Comprehensive tests
14. Token validation - Decent coverage
15. Basic integration flows - Working

---

## Test Execution Statistics

### Current Performance
- **Total Tests:** ~300
- **Unit Tests:** ~200 (30 seconds)
- **Integration Tests:** ~100 (4 minutes)
- **Total Time:** ~4.5 minutes
- **Pass Rate:** 98%+
- **Flaky Tests:** ~2%

### Target Performance
- **Total Tests:** ~600
- **Unit Tests:** ~450 (<1 minute with parallel)
- **Integration Tests:** ~150 (<4 minutes)
- **Total Time:** <5 minutes
- **Pass Rate:** 100%
- **Flaky Tests:** 0%

---

## Quick Action Priorities

### Week 1 (Must Do)
1. ✅ Create upstream provider test infrastructure
2. ✅ Add admin endpoint tests
3. ✅ Add cost calculation tests
4. ✅ Audit reserved balance handling

### Week 2 (Should Do)
5. Add proxy core unit tests
6. Add middleware tests
7. Add auth edge case tests
8. Add concurrency test suite

### Week 3-4 (Important)
9. Add NIP-91 tests
10. Add discovery tests
11. Improve test fixtures
12. Add property-based tests

### Week 5-6 (Nice to Have)
13. Add performance tests
14. Add contract tests
15. Add mutation testing
16. Improve test documentation

---

## Coverage by File Size

| File | LOC | Tests | Coverage | Tests Needed |
|------|-----|-------|----------|--------------|
| `proxy.py` | 300+ | 0 unit | 35% | ~40 tests |
| `nip91.py` | 575 | 0 | 0% | ~40 tests |
| `auth.py` | 662 | 4 unit | 65% | ~30 tests |
| `discovery.py` | 400 | 0 unit | 30% | ~30 tests |
| `algorithm.py` | 301 | 15 unit | 85% | ~5 tests |
| `balance.py` | 244 | 0 unit | 70% | ~20 tests |
| `middleware.py` | 127 | 0 | 0% | ~20 tests |
| `cost_caculation.py` | 157 | 0 | 50% | ~30 tests |
| `price.py` | 163 | 0 | 30% | ~15 tests |

**Total new tests needed:** ~450 tests  
**Estimated effort:** 4-6 weeks (1-2 engineers)

---

## Testing Tools Status

| Tool | Status | Usage | Notes |
|------|--------|-------|-------|
| pytest | ✅ Used | Core test runner | Working well |
| pytest-asyncio | ✅ Used | Async testing | Working well |
| pytest-cov | ⚠️ Partial | Coverage reports | Not in CI |
| pytest-xdist | ❌ Not used | Parallel execution | Should add |
| pytest-benchmark | ❌ Not used | Performance testing | Should add |
| hypothesis | ❌ Not used | Property testing | Should add |
| factory-boy | ❌ Not used | Test data | Should add |
| mutmut | ❌ Not used | Mutation testing | Should add |

---

**Last Updated:** 2025-11-16  
**Review Schedule:** Weekly during test expansion  
**Target Completion:** End of Week 6
