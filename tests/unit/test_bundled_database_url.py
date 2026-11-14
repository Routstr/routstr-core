"""DATABASE_URL resolution on the compose stack's bundled PostgreSQL."""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
BUNDLED = "postgresql+asyncpg://routstr@postgres:5432/routstr"


def _resolve(env: dict[str, str]) -> list[str]:
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DATABASE_URL", "BUNDLED_"))
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from routstr.core import db; print(db.DATABASE_URL); print(db.BUNDLED_DATABASE)",
        ],
        cwd=ROOT,
        env={**base, **env},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-2:]


def test_bundled_url_takes_its_password_from_the_file(tmp_path: Path) -> None:
    password = tmp_path / "password"
    password.write_text("p@ss/w:rd\n")

    url, bundled = _resolve(
        {
            "BUNDLED_DATABASE_URL": BUNDLED,
            "BUNDLED_DATABASE_PASSWORD_FILE": str(password),
        }
    )

    assert url == "postgresql+asyncpg://routstr:p%40ss%2Fw%3Ard@postgres:5432/routstr"
    assert bundled == "True"


def test_bundled_password_keeps_surrounding_spaces(tmp_path: Path) -> None:
    password = tmp_path / "password"
    password.write_text(" sp \n")

    url, _ = _resolve(
        {
            "BUNDLED_DATABASE_URL": BUNDLED,
            "BUNDLED_DATABASE_PASSWORD_FILE": str(password),
        }
    )

    assert make_url(url).password == " sp "


def test_database_url_wins_over_the_bundled_database(tmp_path: Path) -> None:
    password = tmp_path / "password"
    password.write_text("secret")

    url, bundled = _resolve(
        {
            "DATABASE_URL": "sqlite+aiosqlite:///data/custom.db",
            "BUNDLED_DATABASE_URL": BUNDLED,
            "BUNDLED_DATABASE_PASSWORD_FILE": str(password),
        }
    )

    assert url == "sqlite+aiosqlite:///data/custom.db"
    assert bundled == "False"


def test_without_compose_the_default_stays_sqlite() -> None:
    assert _resolve({}) == ["sqlite+aiosqlite:///keys.db", "False"]
