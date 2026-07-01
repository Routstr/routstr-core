from __future__ import annotations

import re
from itertools import count
from typing import Collection

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .db import UpstreamProviderRow

_SLUG_BASE_PATTERN = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LENGTH = 64


def provider_slug_base(provider_type: str) -> str:
    """Return a deterministic slug base for a provider type."""
    base = _SLUG_BASE_PATTERN.sub("-", provider_type.lower()).strip("-")
    if not base:
        base = "provider"
    elif base.isdigit():
        base = f"provider-{base}"
    elif len(base) < 3:
        base = f"{base}-provider"

    if len(base) > _MAX_SLUG_LENGTH:
        base = base[:_MAX_SLUG_LENGTH].rstrip("-") or "provider"
    return base


def provider_slug_candidate(base: str, suffix_number: int) -> str:
    if suffix_number == 1:
        return base

    suffix = f"-{suffix_number}"
    max_base_length = _MAX_SLUG_LENGTH - len(suffix)
    return f"{base[:max_base_length].rstrip('-')}{suffix}"


async def allocate_unique_provider_slug(
    session: AsyncSession,
    provider_type: str,
    reserved_slugs: Collection[str] = (),
) -> str:
    """Allocate a stable, deterministic provider slug.

    The first provider of a type gets ``openai``; later collisions get
    ``openai-2``, ``openai-3``, etc. ``reserved_slugs`` covers rows staged in
    memory but not flushed yet, such as settings/env seeding.
    """
    base = provider_slug_base(provider_type)
    reserved = {slug.lower() for slug in reserved_slugs}

    for suffix_number in count(1):
        candidate = provider_slug_candidate(base, suffix_number)
        if candidate in reserved:
            continue

        result = await session.exec(
            select(UpstreamProviderRow).where(UpstreamProviderRow.slug == candidate)
        )
        if result.first() is None:
            return candidate

    raise RuntimeError("unreachable")
