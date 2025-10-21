# Code Structure

This guide provides a detailed overview of Routstr Core's codebase organization and key modules.

## Directory Layout

```
routstr-core/
├── routstr/                    # Main application package
│   ├── __init__.py            # Package initialization, loads .env
│   ├── auth.py                # Authentication and authorization
│   ├── balance.py             # Balance management endpoints
│   ├── discovery.py           # Nostr relay discovery
│   ├── proxy.py               # Request proxying logic
│   ├── wallet.py              # Cashu wallet operations
│   │
│   ├── core/                  # Core infrastructure
│   │   ├── __init__.py
│   │   ├── admin.py          # Admin dashboard and API
│   │   ├── db.py             # Database models and connection
│   │   ├── exceptions.py     # Custom exception classes
│   │   ├── logging.py        # Structured logging setup
│   │   ├── main.py           # FastAPI app initialization
│   │   └── middleware.py     # HTTP middleware components
│   │
│   └── payment/               # Payment processing
│       ├── __init__.py
│       ├── cost_calculation.py # Usage cost calculation
│       ├── helpers.py         # Payment utilities
│       ├── lnurl.py          # Lightning URL support
│       ├── models.py         # Model pricing management
│       ├── price.py          # BTC/USD price handling
│       └── x_cashu.py        # Cashu header protocol
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
│   └── models_meta.py       # Fetch model pricing
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
# Loads environment variables
import dotenv
dotenv.load_dotenv()

# Exports FastAPI app
from .core.main import app as fastapi_app
```

#### `routstr/core/main.py`

```python
# FastAPI application setup
app = FastAPI(
    title="Routstr Node",
    lifespan=lifespan,  # Manages startup/shutdown
)

# Middleware registration
app.add_middleware(CORSMiddleware, ...)
app.add_middleware(LoggingMiddleware)

# Router inclusion
app.include_router(admin_router)
app.include_router(balance_router)
app.include_router(proxy_router)
```

### Authentication Module

#### `routstr/auth.py`

Handles API key validation and authorization:

```python
class APIKeyAuth:
    """FastAPI dependency for API key authentication"""
    
    async def __call__(self, request: Request) -> APIKey:
        # Extract and validate API key
        # Check balance
        # Return authenticated key object

# Usage in routes:
@router.get("/protected")
async def protected_route(api_key: APIKey = Depends(APIKeyAuth())):
    pass
```

Key functions:

- `create_api_key()` - Generate new API keys
- `validate_api_key()` - Verify and retrieve key
- `check_balance()` - Ensure sufficient funds
- `update_last_used()` - Track usage

### Payment Processing

#### `routstr/payment/cost_calculation.py`

Calculates request costs:

```python
def calculate_request_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    **kwargs
) -> CostData:
    """Calculate cost in millisatoshis"""
    # Model-based or fixed pricing
    # Token counting
    # Fee application
    # Currency conversion
```

#### `routstr/payment/models.py`

Manages model pricing data:

```python
class ModelPrice:
    id: str
    name: str
    pricing: dict[str, float]  # USD prices
    context_length: int
    
# Global model registry
MODELS: dict[str, ModelPrice] = load_models()

# Dynamic price updates
async def update_sats_pricing():
    """Background task to update BTC prices"""
```

#### `routstr/payment/x_cashu.py`

Implements Cashu payment protocol:

```python
class XCashuHandler:
    """Handle x-cashu header payments"""
    
    async def process_request_payment(
        self, 
        token: str,
        estimated_cost: int
    ) -> PaymentResult:
        # Validate token
        # Check minimum amount
        # Process payment
        # Generate change
```

### Request Proxying

#### `routstr/proxy.py`

Core proxy functionality:

```python
@router.api_route("/{path:path}", methods=ALL_METHODS)
async def proxy_request(
    request: Request,
    path: str,
    api_key: APIKey = Depends(APIKeyAuth())
) -> Response:
    """Forward requests to upstream provider"""
    # Build upstream request
    # Stream response
    # Track usage
    # Deduct costs
```

Key features:

- Streaming support
- Header preservation
- Error handling
- Usage tracking

### Database Layer

#### `routstr/core/db.py`

SQLModel definitions:

```python
class APIKey(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    key_hash: str = Field(index=True, unique=True)
    balance: int  # millisatoshis
    total_deposited: int = 0
    total_spent: int = 0
    created_at: datetime
    expires_at: datetime | None = None
    metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))

class Transaction(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    api_key_id: int = Field(foreign_key="apikey.id")
    amount: int  # can be negative
    balance_after: int
    type: TransactionType
    description: str
    timestamp: datetime
```

### Admin Interface

#### `routstr/core/admin.py`

Web dashboard and admin API:

```python
@admin_router.get("/admin/")
async def admin_dashboard(request: Request):
    """Render admin HTML interface"""
    # Authentication check
    # Load statistics
    # Render template

@admin_router.post("/admin/api/withdraw")
async def withdraw_balance(
    api_key: str,
    amount: int | None = None
) -> WithdrawalResponse:
    """Generate eCash token for withdrawal"""
```

Features:

- HTML dashboard
- API key management
- Balance withdrawals
- Usage statistics

### Wallet Integration

#### `routstr/wallet.py`

Cashu wallet operations:

```python
class WalletManager:
    """Manage Cashu wallet instances"""
    
    async def redeem_token(
        self, 
        token: str,
        mint_url: str | None = None
    ) -> int:
        """Redeem eCash token and return value"""
        
    async def create_token(
        self,
        amount: int,
        mint_url: str
    ) -> str:
        """Create eCash token for withdrawal"""
```

### Utility Modules

#### `routstr/core/logging.py`

Structured logging configuration:

```python
def setup_logging():
    """Configure JSON structured logging"""
    # Set log level
    # Configure formatters
    # Add handlers
    
class RequestIdMiddleware:
    """Add request ID to all logs"""
```

#### `routstr/core/middleware.py`

HTTP middleware components:

```python
class LoggingMiddleware:
    """Log all HTTP requests/responses"""
    
class ErrorHandlingMiddleware:
    """Consistent error responses"""
```

#### `routstr/core/exceptions.py`

Custom exception hierarchy:

```python
class RoustrError(Exception):
    """Base exception with error details"""
    status_code: int
    error_type: str
    detail: str

class PaymentError(RoustrError):
    """Payment-related errors"""

class UpstreamError(RoustrError):
    """Upstream API errors"""
```

## Configuration Files

### `pyproject.toml`

Project metadata and dependencies:

```toml
[project]
name = "routstr"
version = "0.1.4b"
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
async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session

# Use in routes
@router.get("/items")
async def get_items(db: AsyncSession = Depends(get_db)):
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
