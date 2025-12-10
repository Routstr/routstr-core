from typing import TYPE_CHECKING

import httpx

from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow


class OpenRouterUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for OpenRouter API."""

    provider_type = "openrouter"
    default_base_url = "https://openrouter.ai/api/v1"
    platform_url = "https://openrouter.ai/settings/keys"

    def __init__(self, api_key: str, provider_fee: float = 1.06):
        """Initialize OpenRouter provider with API key.

        Args:
            api_key: OpenRouter API key for authentication
            provider_fee: Provider fee multiplier (default 1.06 for 6% fee)
        """
        super().__init__(
            base_url=self.default_base_url, api_key=api_key, provider_fee=provider_fee
        )

    @classmethod
    def from_db_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "OpenRouterUpstreamProvider":
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "OpenRouter",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
            "can_show_balance": True,
        }

    async def fetch_models(self) -> list[Model]:
        """Fetch all OpenRouter models."""
        models_data = await async_fetch_openrouter_models()
        return [Model(**model) for model in models_data]  # type: ignore

    async def get_balance(self) -> float | None:
        """Get the current account balance from OpenRouter.

        Returns:
            Float representing the balance amount (in credits/USD), or None if unavailable.
        """
        url = f"{self.base_url}/credits"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()

                credits_data = data.get("data", {})
                total_credits = float(credits_data.get("total_credits", 0.0))
                total_usage = float(credits_data.get("total_usage", 0.0))

                return total_credits - total_usage
        except Exception:
            return None
