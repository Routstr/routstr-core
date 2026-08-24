"""Bounded local cache for failed Cashu token redemptions."""

from __future__ import annotations

import math
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import node_coordination

TERMINAL_REDEMPTION_CODES: frozenset[str] = frozenset(
    {
        "cashu_token_already_spent",
        "invalid_cashu_token",
        "cashu_token_zero_value",
        "cashu_token_swap_fees_exceed_amount",
    }
)
TRANSIENT_REDEMPTION_CODES: frozenset[str] = frozenset(
    {
        "cashu_mint_rate_limited",
        "cashu_source_mint_unreachable",
        "cashu_mint_unreachable",
    }
)

DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_TTL_SECONDS = 24 * 60 * 60
TRANSIENT_TTL_SECONDS = 30.0
MAX_TRANSIENT_TTL_SECONDS = 60.0
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CachedRedemptionFailure:
    status_code: int
    error_type: str
    message: str
    code: str


@dataclass(frozen=True)
class _PersistedFailure:
    expires_at: float
    monotonic_until: float | None
    boot_id: str | None
    ttl_seconds: float | None
    failure: CachedRedemptionFailure


class RedemptionNegativeCache:
    """Memory LRU with an optional node-shared file tier."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        *,
        storage_dir: Path | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._storage_dir = storage_dir
        self._entries: OrderedDict[str, tuple[float, CachedRedemptionFailure]] = (
            OrderedDict()
        )

    def _path(self, hashed_key: str) -> Path | None:
        if self._storage_dir is None or not _HASH_PATTERN.fullmatch(hashed_key):
            return None
        return self._storage_dir / hashed_key

    @staticmethod
    def _decode(value: dict[str, object]) -> _PersistedFailure | None:
        try:
            version = value.get("version")
            if version not in (1, 2):
                return None
            expires_at_value = value["expires_at"]
            status_code_value = value["status_code"]
            error_type = value["error_type"]
            message = value["message"]
            code = value["code"]
        except KeyError:
            return None
        if (
            not isinstance(expires_at_value, (int, float))
            or isinstance(expires_at_value, bool)
            or not isinstance(status_code_value, int)
            or isinstance(status_code_value, bool)
        ):
            return None
        expires_at = float(expires_at_value)
        status_code = status_code_value
        if (
            not math.isfinite(expires_at)
            or not 100 <= status_code <= 599
            or not isinstance(error_type, str)
            or not isinstance(message, str)
            or not isinstance(code, str)
            or len(error_type) > 128
            or len(message) > 1024
            or len(code) > 128
        ):
            return None

        monotonic_until: float | None = None
        boot_id: str | None = None
        ttl_seconds: float | None = None
        if version == 2:
            monotonic_value = value.get("monotonic_until")
            ttl_value = value.get("ttl_seconds")
            boot_id_value = value.get("boot_id")
            if (
                not isinstance(monotonic_value, (int, float))
                or isinstance(monotonic_value, bool)
                or not isinstance(ttl_value, (int, float))
                or isinstance(ttl_value, bool)
                or not isinstance(boot_id_value, (str, type(None)))
            ):
                return None
            monotonic_until = float(monotonic_value)
            ttl_seconds = float(ttl_value)
            if (
                not math.isfinite(monotonic_until)
                or not math.isfinite(ttl_seconds)
                or ttl_seconds <= 0
            ):
                return None
            boot_id = boot_id_value

        return _PersistedFailure(
            expires_at=expires_at,
            monotonic_until=monotonic_until,
            boot_id=boot_id,
            ttl_seconds=ttl_seconds,
            failure=CachedRedemptionFailure(
                status_code=status_code,
                error_type=error_type,
                message=message,
                code=code,
            ),
        )

    def _remember(
        self, hashed_key: str, failure: CachedRedemptionFailure, ttl_seconds: float
    ) -> None:
        if hashed_key in self._entries:
            del self._entries[hashed_key]
        elif len(self._entries) >= self._max_entries:
            self._entries.popitem(last=False)
        self._entries[hashed_key] = (self._clock() + ttl_seconds, failure)

    def get(self, hashed_key: str) -> CachedRedemptionFailure | None:
        entry = self._entries.get(hashed_key)
        if entry is not None:
            expires_at, failure = entry
            if self._clock() < expires_at:
                self._entries.move_to_end(hashed_key)
                return failure
            del self._entries[hashed_key]

        path = self._path(hashed_key)
        if path is None:
            return None
        decoded = node_coordination.read_json(path)
        parsed = self._decode(decoded) if decoded is not None else None
        if parsed is None:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        if (
            parsed.monotonic_until is not None
            and parsed.boot_id is not None
            and parsed.boot_id == node_coordination.NODE_BOOT_ID
        ):
            remaining = parsed.monotonic_until - self._clock()
        else:
            remaining = parsed.expires_at - self._wall_clock()
        maximum_ttl = (
            MAX_TRANSIENT_TTL_SECONDS
            if parsed.failure.code in TRANSIENT_REDEMPTION_CODES
            else self._ttl_seconds
        )
        if parsed.ttl_seconds is not None:
            maximum_ttl = min(maximum_ttl, parsed.ttl_seconds)
        if not math.isfinite(remaining) or remaining <= 0:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        remaining = min(remaining, maximum_ttl)
        if (
            node_coordination.NODE_BOOT_ID is not None
            and parsed.boot_id != node_coordination.NODE_BOOT_ID
        ):
            try:
                self._write_shared(path, parsed.failure, remaining)
            except OSError:
                pass
        self._remember(hashed_key, parsed.failure, remaining)
        return parsed.failure

    def _write_shared(
        self,
        path: Path,
        failure: CachedRedemptionFailure,
        ttl_seconds: float,
    ) -> None:
        node_coordination.write_json(
            path,
            {
                "version": 2,
                "boot_id": node_coordination.NODE_BOOT_ID,
                "monotonic_until": self._clock() + ttl_seconds,
                "expires_at": self._wall_clock() + ttl_seconds,
                "ttl_seconds": ttl_seconds,
                "status_code": failure.status_code,
                "error_type": failure.error_type,
                "message": failure.message,
                "code": failure.code,
            },
        )

    def put(
        self,
        hashed_key: str,
        failure: CachedRedemptionFailure,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("ttl_seconds must be positive and finite")
        if failure.code in TRANSIENT_REDEMPTION_CODES:
            ttl = min(ttl, MAX_TRANSIENT_TTL_SECONDS)
        self._remember(hashed_key, failure, ttl)
        path = self._path(hashed_key)
        if path is None:
            return
        try:
            self._write_shared(path, failure, ttl)
            self._trim_shared()
        except OSError:
            # The memory tier is sufficient for correctness in this process.
            return

    def _trim_shared(self) -> None:
        if self._storage_dir is None:
            return
        try:
            files = [
                entry for entry in os.scandir(self._storage_dir) if entry.is_file()
            ]
        except OSError:
            return
        if len(files) <= self._max_entries:
            return
        dated_files: list[tuple[float, os.DirEntry[str]]] = []
        for entry in files:
            try:
                dated_files.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
        dated_files.sort(key=lambda item: item[0])
        for _, entry in dated_files[: len(dated_files) - self._max_entries]:
            try:
                os.unlink(entry.path)
            except OSError:
                pass

    def discard(self, hashed_key: str) -> None:
        self._entries.pop(hashed_key, None)
        path = self._path(hashed_key)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def clear(self, *, shared: bool = False) -> None:
        self._entries.clear()
        if not shared or self._storage_dir is None:
            return
        try:
            files = list(os.scandir(self._storage_dir))
        except OSError:
            return
        for entry in files:
            if entry.is_file() and _HASH_PATTERN.fullmatch(entry.name):
                try:
                    os.unlink(entry.path)
                except OSError:
                    pass

    def __len__(self) -> int:
        return len(self._entries)


redemption_negative_cache = RedemptionNegativeCache(
    storage_dir=node_coordination.NODE_STATE_DIR / "redemption-failures"
)
