from typing import TYPE_CHECKING

from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow


class FireworksUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for Fireworks.ai API."""

    provider_type = "fireworks"
    default_base_url = "https://api.fireworks.ai/inference/v1"
    platform_url = "https://app.fireworks.ai/settings/users/api-keys"

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        super().__init__(
            base_url=self.default_base_url, api_key=api_key, provider_fee=provider_fee
        )

    @classmethod
    def from_db_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "FireworksUpstreamProvider":
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Fireworks",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'fireworks/' prefix for Fireworks API compatibility."""
        return model_id.split("/")[-1]
