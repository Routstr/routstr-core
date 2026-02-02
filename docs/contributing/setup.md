# Development Setup

This guide will help you set up a development environment for contributing to Routstr Core.

## Prerequisites

Before you begin, ensure you have:

- **Python 3.11+** - Required for type hints and modern features
- **Git** - For version control
- **Docker** (optional) - For running integration tests
- **Make** - For running development commands

## Quick Start

### 1. Fork and Clone

First, fork the repository on GitHub, then clone your fork:

```bash
git clone https://github.com/YOUR_USERNAME/routstr-core.git
cd routstr-core
```

### 2. Set Up Environment

Run the setup command:

```bash
make setup
```

This will:

- ✅ Install [uv](https://github.com/astral-sh/uv) if not present
- ✅ Create a virtual environment
- ✅ Install all dependencies
- ✅ Install dev tools (mypy, ruff, pytest)
- ✅ Install project in editable mode

### 3. Configure Environment

Create your environment file:

```bash
cp .env.example .env
```

Edit `.env` with your configuration:

```bash
# Minimum required for development
UPSTREAM_BASE_URL=https://api.openai.com/v1
UPSTREAM_API_KEY=your-api-key  # Optional for mock testing
ADMIN_PASSWORD=development-password
DATABASE_URL=sqlite+aiosqlite:///dev.db
```

### 4. Verify Installation

Run these commands to verify your setup:

```bash
# Check dependencies
make check-deps

# Run unit tests
make test-unit

# Start development server
make dev
```

## Development Workflow

### Running the Server

For development with auto-reload:

```bash
make dev
# Server starts at http://localhost:8000
# Auto-reloads on code changes
```

For production-like environment:

```bash
make run
```

### Code Quality

Before committing, always run:

```bash
# Format code
make format

# Check linting
make lint

# Type checking
make type-check

# All checks at once
make check
```

### Testing

Run different test suites:

```bash
# Unit tests only (fast)
make test-unit

# Integration tests with mocks
make test-integration

# All tests
make test

# With coverage report
make test-coverage

# Run specific test
uv run pytest tests/unit/test_auth.py::test_token_validation -v
```

### Database Management

Work with database migrations:

```bash
# Create new migration
make db-migrate

# Apply migrations
make db-upgrade

# Rollback one migration
make db-downgrade

# View current revision
make db-current
```

## Project Structure

Understanding the codebase:

```
routstr-core/
├── routstr/                    # Main package
│   ├── __init__.py
│   ├── algorithm.py           # Provider selection algorithms
│   ├── auth.py                # Authentication logic
│   ├── balance.py             # Balance management API
│   ├── discovery.py           # Nostr discovery
│   ├── lightning.py           # Lightning invoice handling
│   ├── nip91.py               # Node announcement implementation
│   ├── proxy.py               # Request proxying
│   ├── wallet.py              # Cashu wallet integration
│   │
│   ├── core/                  # Core modules
│   │   ├── admin.py          # Admin dashboard API
│   │   ├── db.py             # Database models (SQLModel)
│   │   ├── exceptions.py     # Custom exceptions
│   │   ├── log_manager.py    # Log management
│   │   ├── logging.py        # Logging setup
│   │   ├── main.py           # FastAPI app entry
│   │   ├── middleware.py     # HTTP middleware
│   │   └── settings.py       # Configuration
│   │
│   ├── payment/               # Payment processing
│   │   ├── cost_calculation.py
│   │   ├── helpers.py
│   │   ├── lnurl.py          # LNURL support
│   │   ├── models.py         # Model pricing
│   │   └── price.py          # BTC/USD rates
│   │
│   └── upstream/              # Upstream providers
│       ├── base.py           # Base provider class
│       ├── helpers.py        # Shared utilities
│       ├── openai.py         # OpenAI
│       ├── anthropic.py      # Anthropic
│       ├── gemini.py         # Google Gemini
│       ├── openrouter.py     # OpenRouter
│       └── ...               # More providers
│
├── ui/                        # Admin dashboard (Next.js)
│   ├── app/                  # Next.js app router
│   │   ├── page.tsx         # Landing page
│   │   ├── balances/        # Balance management
│   │   ├── logs/            # Request logs viewer
│   │   ├── model/           # Model configuration
│   │   ├── providers/       # Upstream providers
│   │   ├── settings/        # Node settings
│   │   └── transactions/    # Transaction history
│   ├── components/           # React components
│   │   ├── ui/              # shadcn/ui primitives
│   │   ├── landing/         # Landing page components
│   │   └── settings/        # Settings components
│   └── lib/                  # Utilities & API client
│       ├── api/             # Backend API client
│       ├── auth/            # Auth context
│       └── hooks/           # React hooks
│
├── tests/                     # Test suite
├── migrations/                # Alembic migrations
├── scripts/                   # Utility scripts
├── docs/                      # Documentation
├── examples/                  # Usage examples
│
├── Makefile                   # Dev commands
├── pyproject.toml             # Project config
└── compose.yml                # Docker setup
```

### Admin Dashboard (UI)

The admin dashboard is a Next.js app using:

- **Next.js 14** with App Router
- **shadcn/ui** for components
- **Tailwind CSS** for styling
- **pnpm** for package management

```bash
# Development
cd ui
pnpm install
pnpm dev          # http://localhost:3000

# Build for production
pnpm build
```

The UI is served by the FastAPI backend at `/admin/` when built. Use `make build-ui` to build and copy to the backend.

## Common Tasks

### Adding a New Endpoint

1. Create route in appropriate module
2. Add request/response models
3. Write unit tests
4. Update API documentation
5. Add integration tests

Example:

```python
# In routstr/core/main.py or appropriate router
@app.get("/v1/stats")
async def get_stats(
    user: User = Depends(get_current_user)
) -> StatsResponse:
    """Get usage statistics for the current user."""
    # Implementation
    pass
```

### Adding a Database Model

1. Define model in `routstr/core/db.py`
2. Create migration: `make db-migrate`
3. Review generated migration
4. Apply: `make db-upgrade`

Example:

```python
class Transaction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    api_key_id: int = Field(foreign_key="apikey.id")
    amount: int  # millisatoshis
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    description: str
```

### Writing Tests

Follow the AAA pattern:

```python
async def test_balance_deduction():
    # Arrange
    api_key = await create_test_api_key(balance=1000)
    
    # Act
    result = await deduct_balance(api_key.key, amount=100)
    
    # Assert
    assert result.success
    assert result.new_balance == 900
    assert result.deducted == 100
```

## Development Tools

### Makefile Commands

Key commands for development:

```bash
make help         # Show all commands
make setup        # Initial setup
make dev          # Run dev server
make test         # Run all tests
make lint         # Check code style
make format       # Fix code style
make type-check   # Check types
make clean        # Clean temp files
make docker-build # Build Docker image
```

### IDE Setup

#### VS Code

Recommended extensions:

- Python
- Pylance
- Ruff
- GitLens

Settings (`.vscode/settings.json`):

```json
{
    "python.linting.enabled": true,
    "python.linting.ruffEnabled": true,
    "python.formatting.provider": "ruff",
    "python.analysis.typeCheckingMode": "strict",
    "editor.formatOnSave": true
}
```

#### PyCharm

1. Set Python interpreter to uv venv
2. Enable type checking
3. Configure Ruff as external tool
4. Set up file watchers for formatting

### Debugging

#### Debug Server

```bash
# Run with debug logging
LOG_LEVEL=DEBUG make dev

# Or with debugger
uv run python -m debugpy --listen 5678 --wait-for-client \
    -m uvicorn routstr:fastapi_app --reload
```

#### Debug Tests

```bash
# Run specific test with output
uv run pytest tests/unit/test_auth.py -v -s

# With debugger
uv run pytest tests/unit/test_auth.py --pdb
```

## Troubleshooting

### Common Issues

**Import Errors**

```bash
# Ensure project is installed in editable mode
uv sync
uv pip install -e .
```

**Database Errors**

```bash
# Reset database
rm dev.db
make db-upgrade
```

**Type Checking Fails**

```bash
# Clear mypy cache
make clean
make type-check
```

**Tests Fail Locally**

```bash
# Ensure test dependencies are installed
uv sync --dev

# Check for leftover test data
rm -rf test_*.db
```

### Getting Help

- Check existing [GitHub Issues](https://github.com/routstr/routstr-core/issues)
- Ask in [GitHub Discussions](https://github.com/routstr/routstr-core/discussions)
- Read the [Architecture Guide](architecture.md)

## Next Steps

Now that you're set up:

1. Read the [Architecture Overview](architecture.md)
2. Check [open issues](https://github.com/routstr/routstr-core/issues)
3. Start with a small contribution

Happy coding! 🚀
