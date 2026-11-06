"""Provider model catalogs shared between workers through the database.

A snapshot is only ever an optimisation or a fallback: every read and write here
swallows its own failures so a database hiccup cannot break a model refresh.
"""

from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy.exc import IntegrityError

from ..core import get_logger
from ..core.db import UpstreamModelSnapshot, create_session
from ..payment.models import Model

logger = get_logger(__name__)


def provider_config_hash(
    provider_type: str | None,
    base_url: str,
    api_key: str,
    catalog_settings: dict[str, object] | None = None,
) -> str:
    """Fingerprint of what decides a provider's catalog.

    A snapshot taken before an admin changed the URL or key, or left behind by a
    deleted provider whose id was reused, must not be served for the new one.
    """
    # Store only the digest, never credentials or provider settings themselves.
    material = json.dumps(
        [provider_type or "", base_url, api_key, catalog_settings or {}],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


async def load_snapshot(
    provider_id: int, config_hash: str
) -> tuple[list[Model], int] | None:
    """The stored catalog and its unix fetch time, if it matches this provider."""
    try:
        async with create_session() as session:
            row = await session.get(UpstreamModelSnapshot, provider_id)
            if row is None or row.config_hash != config_hash:
                return None
            raw, fetched_at = row.models_json, row.fetched_at
        return [Model.parse_obj(item) for item in json.loads(raw)], fetched_at
    except Exception as e:  # noqa: BLE001 - fall back to fetching upstream
        logger.warning(
            "Could not read shared model catalog",
            extra={
                "provider_id": provider_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )
        return None


async def save_snapshot(
    provider_id: int, config_hash: str, models: list[Model]
) -> None:
    """Store a freshly fetched, non-empty catalog for the other workers."""
    payload = json.dumps([model.dict() for model in models])
    now = int(time.time())
    try:
        async with create_session() as session:
            row = await session.get(UpstreamModelSnapshot, provider_id)
            if row is None:
                session.add(
                    UpstreamModelSnapshot(
                        upstream_provider_id=provider_id,
                        config_hash=config_hash,
                        models_json=payload,
                        fetched_at=now,
                    )
                )
            else:
                row.config_hash = config_hash
                row.models_json = payload
                row.fetched_at = now
            try:
                await session.commit()
            except IntegrityError:
                # A sibling inserted the same provider's first snapshot at the
                # same moment; its copy is just as current.
                await session.rollback()
    except Exception as e:  # noqa: BLE001 - this worker still has its own copy
        logger.warning(
            "Could not store shared model catalog",
            extra={
                "provider_id": provider_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )
