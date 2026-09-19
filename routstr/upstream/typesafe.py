"""Upstream provider for the TypeSafe System One API.

TypeSafe serves "System One" decision models (Jev) at a dedicated
``POST /v1/systemone`` endpoint: the request carries a ``state`` and a map of
typed ``questions`` (noul / choice / score), and the response returns one
``answers`` entry per question plus a flat ``usage`` object
(``{"input_tokens": n, "output_tokens": n}``). That usage shape is exactly
what :func:`routstr.payment.usage.normalize_usage` already parses, so billing
needs no dialect handling — the provider's job is catalog assembly (TypeSafe's
``GET /v1/models`` lists model names but no prices) and standard forwarding.

Rates below are USD per token, matching the prices TypeSafe publishes at
https://docs.typesafe.ai/models (input is charged; output tokens are free).
Because TypeSafe's model listing does not carry pricing, the operator's DB
model row is authoritative whenever one exists; these rates seed the row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from ..payment.models import Architecture, Model, Pricing, TopProvider
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow


# USD per token, keyed by the exact `model` value TypeSafe accepts. Aliases
# (jev-latest -> jev-1.13.0) resolve upstream, so one entry per alias keeps
# every advertised id priced even if the alias target moves.
_TYPESAFE_RATES: dict[str, tuple[float, float]] = {
    "jev-latest": (0.042 / 1_000_000, 0.0),
    "jev-preview": (0.042 / 1_000_000, 0.0),
}

_DEFAULT_RATE = (0.042 / 1_000_000, 0.0)

# Jev ingests state once and evaluates all questions against it in parallel.
# The 64k budget covers state + all questions combined; 32k applies to state +
# the single longest question. 64k is the safe reservation context.
_TYPESAFE_CONTEXT_LENGTH = 64_000


class TypeSafeUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider for the TypeSafe System One decision API.

    TypeSafe exposes one evaluation endpoint (``POST /v1/systemone``) shared
    by every model; the request's ``model`` field selects which one answers.
    The generic chat-forwarding machinery in the base class handles the
    request/response plumbing unchanged — the model id sits in the top-level
    ``model`` field like any OpenAI-compatible API, and the response's
    ``usage`` is already in the canonical billing shape.
    """

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
        """Strip a ``typesafe/`` prefix if one was used to namespace the id."""
        return model_id.removeprefix("typesafe/")

    async def fetch_models(self) -> list[Model]:
        """Fetch the model list TypeSafe's account can call.

        ``GET /v1/models`` requires the provider's API key and returns
        ``{"models": [{"name", "description", "release_date"}, ...]}`` —
        aliases only, with no pricing or context fields. Prices come from the
        table above; the operator's DB model row overrides this pricing
        whenever one exists.
        """
        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()

            entries = data.get("models", []) if isinstance(data, dict) else data
            if not isinstance(entries, list):
                return []

            models: list[Model] = []
            seen: set[str] = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if not isinstance(name, str) or not name or name in seen:
                    continue
                seen.add(name)

                rate = _TYPESAFE_RATES.get(name, _DEFAULT_RATE)
                # An unlisted model is priced at the default Jev rate, but a
                # missing entry in the rate table is still imported enabled:
                # TypeSafe only lists models the account may call, and the
                # operator's DB row overrides this pricing anyway.
                created = 0
                release_date = entry.get("release_date")
                if isinstance(release_date, str):
                    try:
                        from datetime import datetime

                        created = int(
                            datetime.fromisoformat(
                                release_date.replace("Z", "+00:00")
                            ).timestamp()
                        )
                    except ValueError:
                        created = 0

                description = entry.get("description")
                if not isinstance(description, str) or not description:
                    description = f"TypeSafe System One model {name}"

                models.append(
                    Model(
                        id=name,
                        name=name,
                        created=created,
                        description=description,
                        context_length=_TYPESAFE_CONTEXT_LENGTH,
                        architecture=Architecture(
                            modality="text->decisions",
                            input_modalities=["text"],
                            output_modalities=["decisions"],
                            tokenizer="Other",
                            instruct_type=None,
                        ),
                        pricing=Pricing(
                            prompt=rate[0],
                            completion=rate[1],
                        ),
                        top_provider=TopProvider(
                            context_length=_TYPESAFE_CONTEXT_LENGTH,
                        ),
                    )
                )
            return models
        except Exception:
            # Catalog fetch failures (bad key, outage) must not break provider
            # initialization; cached/DB models keep serving.
            return []
