from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_backend_docker_context_excludes_generated_and_runtime_state() -> None:
    patterns = {
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    required_patterns = {
        "**/.env",
        "**/.env.*",
        "**/routstr_secret.key",
        "**/.venv",
        "**/__pycache__",
        "**/*.py[cod]",
        "**/.pytest_cache",
        "**/.mypy_cache",
        "**/.ruff_cache",
        "**/.coverage",
        "**/htmlcov",
        "**/*.egg-info",
        "**/build",
        "**/dist",
        "logs",
        "logs.*",
        "**/*.log",
        "**/.wallet*",
        "**/.cashu",
        "**/*.db",
        "**/*.db-*",
        "**/*.sqlite3",
        "**/*.sqlite3-*",
        "**/keys",
        "**/proof_backups",
        "**/relay-data",
        "ui_out",
        "ui/.next",
        "ui/out",
    }

    assert required_patterns <= patterns, (
        "The backend Docker context must exclude local build artifacts and "
        f"runtime state; missing patterns: {sorted(required_patterns - patterns)}"
    )
