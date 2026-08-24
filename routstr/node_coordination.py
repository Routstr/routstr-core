from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Generator

NODE_STATE_DIR = Path(".wallet") / ".routstr-node"
_MAX_STATE_BYTES = 16 * 1024
_MAX_INTEGER_DIGITS = 128


def _parse_int(value: str) -> int:
    if len(value.removeprefix("-")) > _MAX_INTEGER_DIGITS:
        raise ValueError("node coordination integer is too large")
    return int(value)


def _read_boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None
    return value or None


NODE_BOOT_ID = _read_boot_id()


def state_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def read_json(path: Path) -> dict[str, Any] | None:
    fd: int | None = None
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as file:
            fd = None
            payload = file.read(_MAX_STATE_BYTES + 1)
        if len(payload) > _MAX_STATE_BYTES:
            return None
        value = json.loads(payload.decode(), parse_int=_parse_int)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    finally:
        if fd is not None:
            os.close(fd)
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    if len(payload) > _MAX_STATE_BYTES:
        raise ValueError("node coordination state is too large")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def try_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def blocking_lock(path: Path) -> Generator[None, None, None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        unlock(fd)


@asynccontextmanager
async def exclusive_lock(path: Path) -> AsyncGenerator[None, None]:
    fd: int | None = None
    try:
        while fd is None:
            fd = try_lock(path)
            if fd is None:
                await asyncio.sleep(0.05)
        yield
    finally:
        if fd is not None:
            unlock(fd)
