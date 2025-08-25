# Contributing to Routstr Proxy

We welcome contributions to Routstr Proxy! This document provides guidelines and instructions for contributing to the project.

## Table of Contents

- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Code Standards](#code-standards)
- [Testing](#testing)
- [Submitting Changes](#submitting-changes)
- [Project Structure](#project-structure)
- [Documentation](#documentation)
- [Release Process](#release-process)

## Getting Started

### Prerequisites

- Python 3.11 or higher
- [uv](https://docs.astral.sh/uv/) package manager
- Docker and Docker Compose (optional, for integration tests)
- Git

### Development Setup

1. **Fork and clone the repository**

   ```bash
   git clone https://github.com/YOUR_USERNAME/routstr-proxy.git
   cd routstr-proxy
   ```

2. **Set up the development environment**

   ```bash
   make setup
   ```

   This will:
   - Install `uv` if not already installed
   - Create a virtual environment
   - Install all dependencies including dev tools
   - Install the project in editable mode

3. **Configure environment variables**

   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

4. **Verify your setup**

   ```bash
   make check-deps
   make test-unit
   ```

## Code Standards

### Python Style Guide

We use modern Python 3.11+ features and enforce strict type checking:

- **Type Hints**: All functions must have complete type annotations

  ```python
  # ✅ Good
  def calculate_cost(tokens: int, price_per_token: float) -> dict[str, float]:
      return {"total": tokens * price_per_token}
  
  # ❌ Bad
  def calculate_cost(tokens, price_per_token):
      return {"total": tokens * price_per_token}
  ```

- **Type Syntax**: Use Python 3.11+ lowercase types

  ```python
  # ✅ Good
  def process_items(items: list[dict[str, str | None]]) -> dict[str, int]:
      ...
  
  # ❌ Bad
  from typing import List, Dict, Optional
  def process_items(items: List[Dict[str, Optional[str]]]) -> Dict[str, int]:
      ...
  ```

- **Comments**: Only add comments for non-obvious logic. Code should be self-documenting

  ```python
  # ✅ Good - complex business logic explained
  # Apply exponential backoff with jitter to prevent thundering herd
  delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
  
  # ❌ Bad - obvious comment
  # Increment counter by 1
  counter += 1
  ```

### Code Quality Tools

We enforce code quality using:

- **Ruff**: For linting and formatting

  ```bash
  make lint    # Check for issues
  make format  # Auto-fix formatting
  ```

- **Mypy**: For type checking

  ```bash
  make type-check
  ```

### Commit Messages

Follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

```text
<type>(<scope>): <subject>

<body>

<footer>
```

Types:

- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Test additions or fixes
- `chore`: Build process or auxiliary tool changes

Examples:

```text
feat(proxy): add support for streaming responses

fix(wallet): handle expired tokens correctly

docs: update API documentation for v2 endpoints
```

## Testing

### Test Structure

Tests are organized into:

- `tests/unit/` - Fast, isolated unit tests
- `tests/integration/` - Integration tests (can use mocks or real services)

### Running Tests

```bash
# Run all tests (unit + integration with mocks)
make test

# Run specific test suites
make test-unit                    # Unit tests only
make test-integration             # Integration tests with mocks
make test-integration-docker      # Integration tests with real services
make test-performance             # Performance benchmarks

# Advanced testing
make test-coverage                # Generate coverage report
make test-fast                    # Skip slow tests
make test-failed                  # Re-run only failed tests
```

### Writing Tests

1. **Use pytest fixtures** for reusable test setup
2. **Mark async tests** with `@pytest.mark.asyncio`
3. **Use appropriate markers**:

   ```python
   @pytest.mark.slow
   @pytest.mark.requires_docker
   async def test_complex_integration():
       ...
   ```

4. **Follow the AAA pattern**: Arrange, Act, Assert

   ```python
   async def test_token_validation():
       # Arrange
       token = create_test_token(amount=1000)
       
       # Act
       result = await validate_token(token)
       
       # Assert
       assert result.is_valid
       assert result.amount == 1000
   ```

## Submitting Changes

### Pull Request Process

1. **Create a feature branch**

   ```bash
   git checkout -b feat/your-feature-name
   ```

2. **Make your changes**
   - Write code following our standards
   - Add or update tests
   - Update documentation if needed

3. **Run quality checks**

   ```bash
   make lint
   make type-check
   make test
   ```

4. **Commit your changes**
   - Use conventional commit messages
   - Keep commits focused and atomic

5. **Push and create a PR**
   - Push to your fork
   - Create a PR against the `main` branch
   - Fill out the PR template completely
   - Link any related issues

### PR Review Checklist

Before requesting review, ensure:

- [ ] All tests pass
- [ ] Code follows style guidelines
- [ ] Type hints are complete and correct
- [ ] Documentation is updated
- [ ] Commit messages follow conventions
- [ ] No unnecessary changes outside scope

### What to Expect

- Reviews typically happen within 2-3 business days
- Be prepared to make changes based on feedback
- Engage constructively in discussions
- Once approved, a maintainer will merge your PR

## Project Structure

```text
routstr-proxy/
├── routstr/                 # Main application code
│   ├── core/              # Core functionality
│   │   ├── admin.py       # Admin interface
│   │   ├── db.py          # Database models and operations
│   │   ├── logging.py     # Logging configuration
│   │   └── main.py        # FastAPI app initialization
│   ├── payment/           # Payment processing
│   │   ├── cost_calculation.py
│   │   ├── models.py
│   │   └── x_cashu.py     # Cashu integration
│   ├── auth.py            # Authentication
│   ├── proxy.py           # Request proxying logic
│   └── wallet.py          # Wallet management
├── tests/                 # Test suite
│   ├── unit/              # Unit tests
│   └── integration/       # Integration tests
├── scripts/               # Utility scripts
├── compose.yml            # Docker compose for production
├── compose.testing.yml    # Docker compose for testing
├── Makefile              # Development commands
└── pyproject.toml        # Project configuration
```

### Key Components

- **FastAPI Application**: Main API server in `routstr/core/main.py`
- **Database Models**: SQLModel definitions in `routstr/core/db.py`
- **Payment Logic**: Cashu integration and cost calculation in `routstr/payment/`
- **Proxy Handler**: Request forwarding logic in `routstr/proxy.py`

## Documentation

### Code Documentation

- Use descriptive variable and function names
- Add docstrings for public APIs:

  ```python
  async def redeem_token(token: str, mint_url: str) -> RedemptionResult:
      """Redeem a Cashu token and credit the account.
      
      Args:
          token: Base64-encoded Cashu token
          mint_url: URL of the Cashu mint
          
      Returns:
          RedemptionResult with amount and status
          
      Raises:
          TokenInvalidError: If token is malformed or expired
          MintConnectionError: If mint is unreachable
      """
  ```

### API Documentation

- Update OpenAPI schemas when adding endpoints
- Keep `README.md` examples current
- Document environment variables in `.env.example`

### Architecture Decisions

For significant changes, create an ADR (Architecture Decision Record) in `docs/adr/`:

```markdown
# ADR-001: Use SQLite for Local Storage

## Status
Accepted

## Context
We need a simple, embedded database for storing API keys and balances.

## Decision
Use SQLite with SQLModel ORM for type safety and async support.

## Consequences
- No external database required
- Simple deployment
- Limited concurrent write performance
```

## Release Process

### Version Numbering

We use [Semantic Versioning](https://semver.org/):

- MAJOR: Breaking API changes
- MINOR: New features, backwards compatible
- PATCH: Bug fixes and minor improvements

### Release Steps

1. Update version in `pyproject.toml`
2. Update `CHANGELOG.md` with release notes
3. Create a git tag: `git tag -a v1.2.3 -m "Release v1.2.3"`
4. Push tag: `git push origin v1.2.3`
5. GitHub Actions will build and publish Docker images

## Getting Help

- **Issues**: Check existing issues or create a new one
- **Discussions**: Use GitHub Discussions for questions
- **Security**: Report security issues privately to maintainers

## License

By contributing, you agree that your contributions will be licensed under the GPLv3 license.
