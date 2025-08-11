# Makefile for Routstr Proxy

# Detect if we're in a virtual environment
VENV_EXISTS := $(shell test -d .venv && echo 1)
ifeq ($(VENV_EXISTS), 1)
    PYTHON := .venv/bin/python
    PYTEST := .venv/bin/pytest
    RUFF := .venv/bin/ruff
    MYPY := .venv/bin/mypy
    ALEMBIC := .venv/bin/alembic
else
    PYTHON := python
    PYTEST := pytest
    RUFF := ruff
    MYPY := mypy
    ALEMBIC := alembic
endif

.PHONY: help setup test test-unit test-integration test-integration-docker test-all test-fast test-performance clean docker-up docker-down lint format type-check dev-setup check-deps db-upgrade db-downgrade db-current db-history db-migrate db-revision db-heads db-clean

# Default target
help:
	@echo "Available targets:"
	@echo "  make test               - Run all tests (unit + integration with mocks)"
	@echo "  make test-unit          - Run unit tests only"
	@echo "  make test-integration   - Run integration tests with mocks (fast)"
	@echo "  make test-integration-docker - Run integration tests with Docker services"
	@echo "  make test-all           - Run all tests including Docker integration"
	@echo "  make test-fast          - Run fast tests only (skip slow tests)"
	@echo "  make test-performance   - Run performance tests"
	@echo "  make docker-up          - Start Docker test services"
	@echo "  make docker-down        - Stop Docker test services"
	@echo "  make clean              - Clean up test artifacts and caches"
	@echo "  make lint               - Run linting checks"
	@echo "  make format             - Format code with ruff"
	@echo "  make type-check         - Run mypy type checking"
	@echo "  make dev-setup          - Set up development environment"
	@echo "  make check-deps         - Check system dependencies"
	@echo "  make setup              - First-time project setup"
	@echo ""
	@echo "Database migration shortcuts:"
	@echo "  make create-migration   - Auto-generate new migration"
	@echo "  make db-upgrade         - Apply all pending migrations"
	@echo "  make db-downgrade       - Downgrade one migration"

# First-time setup
setup: check-deps dev-setup
	@echo ""
	@echo "🎉 Setup complete! Next steps:"
	@echo "  1. Run tests:         make test"
	@echo "  2. Run integration:   make test-integration-docker"
	@echo "  3. Start developing!"

# Test targets
test: test-unit test-integration

test-unit:
	@echo "🧪 Running unit tests..."
	$(PYTEST) tests/unit/ -v

test-integration:
	@echo "🎭 Running integration tests with mocks..."
	$(PYTEST) tests/integration/ -v

test-integration-docker:
	@echo "🐳 Running integration tests with Docker services..."
	./tests/run_integration.py

test-all: test-unit test-integration-docker

test-fast:
	@echo "⚡ Running fast tests only..."
	$(PYTEST) -m "not slow and not requires_docker" -v

test-performance:
	@echo "📊 Running performance tests..."
	$(PYTEST) tests/integration/ -m "performance" -v -s

# Docker management
docker-up:
	@echo "🚀 Starting Docker test services..."
	docker-compose -f compose.testing.yml up -d
	@echo "Waiting for services to be ready..."
	@sleep 5
	@echo "Services started. Run 'make test-integration-docker' to test."

docker-down:
	@echo "🛑 Stopping Docker test services..."
	docker-compose -f compose.testing.yml down -v

# Code quality
lint:
	@echo "🔍 Running linting checks..."
	$(RUFF) check .
	$(MYPY) router/ --ignore-missing-imports

format:
	@echo "✨ Formatting code..."
	$(RUFF) format .
	$(RUFF) check --fix .

type-check:
	@echo "🔎 Running type checks..."
	$(MYPY) router/ --ignore-missing-imports

# Development setup
dev-setup:
	@echo "🔧 Setting up development environment..."
	@# Check if uv is installed
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "📦 uv not found. Installing uv..."; \
		if command -v curl >/dev/null 2>&1; then \
			curl -LsSf https://astral.sh/uv/install.sh | sh; \
		elif command -v pip >/dev/null 2>&1; then \
			pip install uv; \
		else \
			echo "❌ Neither curl nor pip found. Please install uv manually:"; \
			echo "   Visit https://docs.astral.sh/uv/getting-started/installation/"; \
			exit 1; \
		fi; \
		echo "✅ uv installed successfully!"; \
	else \
		echo "✅ uv is already installed (version: $$(uv --version))"; \
	fi
	uv sync --dev
	uv pip install -e .
	@echo "✅ Development environment ready!"

# Check dependencies
check-deps:
	@echo "🔍 Checking system dependencies..."
	@echo ""
	@echo "Core tools:"
	@printf "  %-18s" "Python:"; if command -v python >/dev/null 2>&1; then python --version; else echo "❌ Not found"; fi
	@printf "  %-18s" "uv:"; if command -v uv >/dev/null 2>&1; then uv --version; else echo "❌ Not found - run 'make dev-setup' to install"; fi
	@printf "  %-18s" "Docker:"; if command -v docker >/dev/null 2>&1; then docker --version; else echo "⚠️  Not found (optional, needed for integration tests)"; fi
	@printf "  %-18s" "Docker Compose:"; if command -v docker-compose >/dev/null 2>&1; then docker-compose --version; else echo "⚠️  Not found (optional, needed for integration tests)"; fi
	@echo ""
	@echo "Development tools:"
	@printf "  %-18s" "pytest:"; if $(PYTEST) --version >/dev/null 2>&1; then $(PYTEST) --version | head -1; else echo "❌ Not found - run 'make dev-setup'"; fi
	@printf "  %-18s" "ruff:"; if $(RUFF) --version >/dev/null 2>&1; then $(RUFF) --version; else echo "❌ Not found - run 'make dev-setup'"; fi
	@printf "  %-18s" "mypy:"; if $(MYPY) --version >/dev/null 2>&1; then $(MYPY) --version; else echo "❌ Not found - run 'make dev-setup'"; fi
	@printf "  %-18s" "alembic:"; if $(ALEMBIC) --version >/dev/null 2>&1; then $(ALEMBIC) --version; else echo "❌ Not found - run 'make dev-setup'"; fi
	@echo ""
	@echo "Virtual environment:"
	@if [ -d ".venv" ]; then \
		echo "  ✅ .venv exists"; \
		echo "  Python: $$(.venv/bin/python --version)"; \
	else \
		echo "  ❌ .venv not found - run 'make dev-setup'"; \
	fi
	@echo ""
	@echo "To set up missing dependencies, run: make dev-setup"

# Cleanup
clean:
	@echo "🧹 Cleaning up..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name ".coverage" -delete
	rm -rf htmlcov/
	rm -rf dist/
	rm -rf build/
	rm -rf *.egg-info
	@echo "✨ Cleanup complete!"

# Database migration management
db-upgrade:
	@echo "⬆️  Applying all pending migrations..."
	$(ALEMBIC) upgrade head
	@echo "✅ Database upgraded to latest revision"

db-downgrade:
	@echo "⬇️  Downgrading one migration..."
	$(ALEMBIC) downgrade -1
	@echo "✅ Database downgraded by one revision"

db-current:
	@echo "📍 Current database revision:"
	$(ALEMBIC) current -v

db-history:
	@echo "📜 Migration history:"
	$(ALEMBIC) history --verbose

db-migrate:
	@echo "🔍 Auto-generating migration from model changes..."
	@read -p "Enter migration message: " msg; \
	$(ALEMBIC) revision --autogenerate -m "$$msg"
	@echo "✅ Migration generated. Review and edit if needed."

db-revision:
	@echo "📝 Creating empty migration file..."
	@read -p "Enter migration message: " msg; \
	$(ALEMBIC) revision -m "$$msg"
	@echo "✅ Empty migration created"

db-heads:
	@echo "🎯 Current migration heads:"
	$(ALEMBIC) heads

db-clean:
	@echo "🧹 Cleaning migration cache files..."
	find migrations/ -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ Migration cache cleaned"

# Advanced testing options
test-coverage:
	@echo "📊 Running tests with coverage..."
	$(PYTEST) --cov=router --cov-report=html --cov-report=term
	@echo "Coverage report generated in htmlcov/"

test-watch:
	@echo "👁️  Running tests in watch mode..."
	$(PYTEST)-watch

test-parallel:
	@echo "🚀 Running tests in parallel..."
	$(PYTEST) -n auto -v

# CI/CD specific targets
ci-test:
	@echo "🤖 Running CI test suite..."
	$(PYTEST) -m "not requires_docker" --tb=short -v

ci-lint:
	@echo "🤖 Running CI linting..."
	$(RUFF) check . --exit-non-zero-on-fix
	$(MYPY) router/ --ignore-missing-imports --no-error-summary

# Debug helpers
test-debug:
	@echo "🐛 Running tests with debugging enabled..."
	$(PYTEST) -vvs --tb=long --pdb-trace

test-failed:
	@echo "🔄 Re-running failed tests..."
	$(PYTEST) --lf -v

# Performance profiling
profile:
	@echo "🔥 Running with profiling..."
	$(PYTHON) -m cProfile -o profile.stats -m pytest tests/integration/test_performance_load.py::TestPerformanceBaseline -v
	@echo "Profile saved to profile.stats. Use '$(PYTHON) -m pstats profile.stats' to analyze."
