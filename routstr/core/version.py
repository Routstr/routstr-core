"""Application version resolution.

Priority order:

1. ``VERSION_SUFFIX`` env var (manual override; preserves prior behaviour).
2. Bare base version when HEAD is on the matching release tag (detected via
   ``GIT_TAG`` env or ``git describe --tags --exact-match HEAD``).
3. ``GIT_COMMIT`` env var (build-time injection) -> ``<base>+g<sha>``.
4. Local ``.git`` lookup (source checkouts) -> ``<base>+g<sha>``.
5. Fallback: bare base version.

The ``+g<sha>`` form is PEP 440 local-version syntax so the result remains a
valid package version.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

BASE_VERSION = "0.4.4"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GIT_TIMEOUT_SECONDS = 2.0


def _run_git(*args: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=_REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_short_sha() -> str | None:
    sha = os.getenv("GIT_COMMIT", "").strip()
    if sha:
        return sha[:7]
    return _run_git("rev-parse", "--short=7", "HEAD")


def _on_tagged_release() -> bool:
    tag = os.getenv("GIT_TAG", "").strip()
    if tag:
        return tag.lstrip("v") == BASE_VERSION
    described = _run_git("describe", "--tags", "--exact-match", "HEAD")
    if described and described.lstrip("v") == BASE_VERSION:
        return True
    return False


@lru_cache(maxsize=1)
def get_version() -> str:
    suffix = os.getenv("VERSION_SUFFIX")
    if suffix is not None:
        return f"{BASE_VERSION}-{suffix}"

    if _on_tagged_release():
        return BASE_VERSION

    sha = _git_short_sha()
    if not sha:
        return BASE_VERSION

    return f"{BASE_VERSION}+g{sha}"


__version__ = get_version()

__all__ = ["BASE_VERSION", "__version__", "get_version"]
