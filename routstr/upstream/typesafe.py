"""Upstream provider for the TypeSafe System One API.

``POST /v1/systemone`` takes ``{state, model, questions}`` and returns
``{model, answers, usage}``. The ``usage`` shape (``input_tokens`` /
``output_tokens``) is what :func:`routstr.payment.usage.normalize_usage`
already parses, so billing needs no dialect handling. This provider only
assembles the catalog: ``GET /v1/models`` lists names without prices.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from ..core.logging import get_logger
from ..payment.models import Architecture, Model, Pricing, TopProvider
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)

# USD per token (https://docs.typesafe.ai/models). Output is free.
_INPUT_RATE_USD = 0.042 / 1_000_000
_OUTPUT_RATE_USD = 0.0

# The listing returns aliases only; versioned ids are accepted but unlisted.
_VERSIONED_MODEL_IDS = ("jev-1.13.0",)

_CONTEXT_LENGTH = 64_000
_MODELS_TIMEOUT_SECONDS = 30.0


def _parse_release_date(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _build_model(name: str, entry: dict[str, Any] | None = None) -> Model:
    entry = entry or {}
    description = entry.get("description")
    if not isinstance(description, str) or not description:
        description = f"TypeSafe System One model {name}"

    return Model(
        id=name,
        name=name,
        created=_parse_release_date(entry.get("release_date")),
        description=description,
        context_length=_CONTEXT_LENGTH,
        architecture=Architecture(
            modality="text->decisions",
            input_modalities=["text"],
            output_modalities=["decisions"],
            tokenizer="Other",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=_INPUT_RATE_USD, completion=_OUTPUT_RATE_USD),
        top_provider=TopProvider(context_length=_CONTEXT_LENGTH),
    )


def _models_from_listing(data: object) -> list[Model]:
    entries = data.get("models", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        entries = []

    models: dict[str, Model] = {}
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name and name not in models:
            models[name] = _build_model(name, entry)

    for name in _VERSIONED_MODEL_IDS:
        if name not in models:
            models[name] = _build_model(name)
    return list(models.values())


class TypeSafeUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider for the TypeSafe System One decision API."""

    provider_type = "typesafe"
    default_base_url = "https://api.typesafe.ai/v1"
    platform_url = "https://docs.typesafe.ai"

    def __init__(self, api_key: str, provider_fee: float = 1.0):
        super().__init__(
            base_url=self.default_base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "TypeSafeUpstreamProvider":
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "TypeSafe",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
            "can_create_account": False,
            "can_topup": False,
            "can_show_balance": False,
        }

    def transform_model_name(self, model_id: str) -> str:
        return model_id.removeprefix("typesafe/")

    async def fetch_models(self) -> list[Model]:
        """Fetch the catalog; return an empty list on failure so init never breaks."""
        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=_MODELS_TIMEOUT_SECONDS) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning(
                "Failed to fetch TypeSafe model catalog",
                extra={"url": url, "error": str(exc)},
                exc_info=True,
            )
            return []

        return _models_from_listing(data)
