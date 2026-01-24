# Code Structure

This guide provides a detailed overview of Routstr Core's codebase organization and key modules.

## Directory Layout

```
routstr-core/
├── routstr/                    # Main application package
│   ├── __init__.py            # Package initialization, exports FastAPI app
│   ├── algorithm.py           # Model selection/mapping logic
│   ├── auth.py                # Bearer/Cashu auth and payment handling
│   ├── balance.py             # Balance management endpoints
│   ├── discovery.py           # Nostr relay discovery
│   ├── lightning.py           # Lightning invoice topups
│   ├── nip91.py               # Node announcement logic
│   ├── proxy.py               # Request proxying logic
│   ├── wallet.py              # Cashu wallet operations
│   │
│   ├── core/                  # Core infrastructure
│   │   ├── __init__.py
│   │   ├── admin.py          # Admin dashboard and API
│   │   ├── db.py             # Database models and connection
│   │   ├── exceptions.py     # Exception handlers
│   │   ├── logging.py        # Structured logging setup
│   │   ├── main.py           # FastAPI app initialization
│   │   └── middleware.py     # HTTP middleware components
│   │
│   ├── payment/               # Payment processing
│   │   ├── __init__.py
│   │   ├── cost_calculation.py # Usage cost calculation
│   │   ├── helpers.py         # Payment utilities
│   │   ├── lnurl.py          # Lightning URL support
│   │   ├── models.py         # Model pricing management
│   │   └── price.py          # BTC/USD price handling
│   │
│   └── upstream/              # Upstream provider integrations
│       ├── base.py           # Base provider logic
│       ├── helpers.py        # Provider init and model refresh
│       └── ...               # Provider implementations
│
├── tests/                     # Test suite
│   ├── __init__.py
│   ├── conftest.py           # Pytest configuration
│   ├── unit/                 # Unit tests
│   └── integration/          # Integration tests
│
├── migrations/               # Alembic database migrations
│   ├── alembic.ini
│   ├── env.py
│   ├── script.py.mako
│   └── versions/             # Migration files
│
├── scripts/                  # Utility scripts
│   ├── models_meta.py       # Fetch model pricing
│   └── ...                  # Build/update helpers
│
├── examples/                 # Example clients
├── testing-clients/          # HTML test clients
├── ui/                       # Next.js admin UI
│
├── docs/                    # Documentation
├── logs/                    # Application logs (git ignored)
│
├── .github/                 # GitHub Actions workflows
├── .env.example            # Environment variable template
├── .gitignore             # Git ignore rules
├── .dockerignore          # Docker ignore rules
├── Dockerfile             # Container definition
├── Makefile               # Development commands
├── README.md              # Project overview
├── alembic.ini            # Migration configuration
├── compose.yml            # Docker Compose setup
├── compose.testing.yml    # Testing environment
├── pyproject.toml         # Project configuration
└── uv.lock               # Locked dependencies
```

## Key Modules

### Application Entry Point

#### `routstr/__init__.py`

```python
from .core.main import app as fastapi_app

__all__ = ["fastapi_app"]
```

#### `routstr/core/main.py`

```python
# FastAPI application setup
app = FastAPI(version=__version__, lifespan=lifespan)

# Middleware registration
app.add_middleware(CORSMiddleware, ...)
app.add_middleware(LoggingMiddleware)

# Router inclusion
app.include_router(models_router)
app.include_router(admin_router)
app.include_router(balance_router)
app.include_router(deprecated_wallet_router)
app.include_router(providers_router)
app.include_router(proxy_router)
```

### Authentication Module

#### `routstr/auth.py`

Handles bearer key validation and payment lifecycle (bearer or Cashu token):

```python
async def validate_bearer_key(
    bearer_key: str,
    session: AsyncSession,
    refund_address: Optional[str] = None,
    key_expiry_time: Optional[int] = None,
) -> ApiKey:
    """Validate bearer API key or redeem Cashu token into a balance."""
```

Key functions:

- `validate_bearer_key()` - Validate API key or Cashu token
- `pay_for_request()` - Reserve max cost before upstream call
- `adjust_payment_for_tokens()` - Adjust final cost after response
- `revert_pay_for_request()` - Refund on upstream failure

### Payment Processing

#### `routstr/payment/cost_calculation.py`

Calculates request costs:

```python
async def calculate_cost(
    response_data: dict, max_cost: int, session: AsyncSession
) -> CostData | MaxCostData | CostDataError:
    """Calculate cost in millisatoshis from response usage or model pricing."""
```

#### `routstr/payment/models.py`

Manages model pricing, database overrides, and pricing refresh:

```python
class Model(BaseModel):
    id: str
    name: str
    pricing: Pricing
    sats_pricing: Pricing | None = None

async def update_sats_pricing():
    """Periodic task to update sats pricing for providers and overrides."""
```

#### `routstr/proxy.py` + `routstr/upstream/*`

The `x-cashu` header is handled by the proxy route and delegated to upstream providers.

### Request Proxying

#### `routstr/proxy.py`

Core proxy functionality:

```python
@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(
    request: Request, path: str, session: AsyncSession = Depends(get_session)
) -> Response | StreamingResponse:
    """Forward requests to upstream provider and charge usage."""
```

Key features:

- Streaming support
- Header preservation
- Error handling
- Usage tracking

### Database Layer

#### `routstr/core/db.py`

SQLModel definitions (selected):

```python
class ApiKey(SQLModel, table=True):
    hashed_key: str = Field(primary_key=True)
    balance: int
    reserved_balance: int = 0
    refund_address: str | None = None
    key_expiry_time: int | None = None
    total_spent: int = 0
    total_requests: int = 0

class LightningInvoice(SQLModel, table=True):
    id: str = Field(primary_key=True)
    bolt11: str
    amount_sats: int
    status: str

class UpstreamProviderRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    provider_type: str
    base_url: str
    api_key: str
```

### Admin Interface

#### `routstr/core/admin.py`

Web dashboard and admin API:

```python
@admin_router.get("/admin")
async def admin_dashboard(request: Request):
    """Render admin HTML interface"""
    # Authentication check
    # Load statistics
    # Render template

@admin_router.post("/admin/withdraw")
async def withdraw_balance(
    request: Request, withdraw_request: WithdrawRequest
) -> dict[str, str]:
    """Generate eCash token for withdrawal"""
```

Features:

- HTML dashboard
- API key management
- Balance withdrawals
- Usage statistics

### Wallet Integration

#### `routstr/wallet.py`

Cashu wallet operations (function-based):

```python
async def recieve_token(token: str) -> tuple[int, str, str]:
    """Redeem eCash token and return amount/unit/mint."""

async def send_token(amount: int, unit: str, mint_url: str | None = None) -> str:
    """Create eCash token for withdrawal."""
```

### Utility Modules

#### `routstr/core/logging.py`

Structured logging configuration:

```python
def setup_logging():
    """Configure JSON structured logging"""

class RequestIdFilter(logging.Filter):
    """Attach request ID to log records."""
```

#### `routstr/core/middleware.py`

HTTP middleware components:

```python
class LoggingMiddleware:
    """Log all HTTP requests/responses"""
```

#### `routstr/core/exceptions.py`

Exception handlers:

```python
async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """HTTP exception handler with request ID"""

async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Fallback exception handler with request ID"""
```

## Configuration Files

### `pyproject.toml`

Project metadata and dependencies:

```toml
[project]
name = "routstr"
version = "0.2.2"
dependencies = [
    "fastapi[standard]>=0.115",
    "sqlmodel>=0.0.24",
    "cashu",
    # ...
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff.lint]
select = ["E", "F", "I"]
```

### `alembic.ini`

Database migration configuration:

```ini
[alembic]
script_location = migrations
prepend_sys_path = .
version_path_separator = os

[loggers]
keys = root,sqlalchemy,alembic
```

### `Makefile`

Development commands:

```makefile
# Setup commands
setup:
    uv sync
    uv pip install -e .

# Development server
dev:
    fastapi dev routstr --host 0.0.0.0

# Testing
test:
    uv run pytest

# Code quality
lint:
    uv run ruff check .
```

## Code Patterns

### Dependency Injection

Using FastAPI's DI system:

```python
# Define dependency
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

# Use in routes
@router.get("/items")
async def get_items(db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Item))
    return result.scalars().all()
```

### Async Context Managers

For resource management:

```python
async with httpx.AsyncClient() as client:
    response = await client.get(url)
    
async with database.transaction():
    # Atomic operations
```

### Type Safety

Leveraging Python 3.11+ features:

```python
# Union types with |
def process(value: str | int) -> dict[str, Any]:
    pass

# Type aliases
Balance = int  # millisatoshis
TokenList = list[dict[str, str]]
```

### Error Handling

Consistent error responses:

```python
try:
    result = await risky_operation()
except SpecificError as e:
    logger.error("Operation failed", exc_info=True)
    raise HTTPException(
        status_code=400,
        detail={
            "error": "specific_error",
            "message": str(e)
        }
    )
```

## Best Practices

### Module Organization

1. **Single Responsibility**: Each module has one clear purpose
2. **Minimal Imports**: Import only what's needed
3. **Circular Dependencies**: Avoid by using dependency injection
4. **Public API**: Expose through `__init__.py`

### Function Design

1. **Type Hints**: Always include complete type annotations
2. **Async First**: Use async/await for I/O operations
3. **Error Handling**: Raise specific exceptions
4. **Documentation**: Docstrings for public functions

### Testing Structure

1. **Mirror Source**: Test structure matches source
2. **Fixtures**: Reusable test data in conftest.py
3. **Mocking**: Mock external dependencies
4. **Coverage**: Aim for >80% coverage

## Next Steps

- Review [Testing Guide](testing.md) for test structure
- Read [Architecture](architecture.md) for system design
