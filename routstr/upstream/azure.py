from typing import TYPE_CHECKING, Mapping

from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow


class AzureUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for Azure OpenAI Service."""

    provider_type = "azure"
    default_base_url = None
    platform_url = "https://portal.azure.com/"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_version: str,
        provider_fee: float = 1.01,
    ):
        """Initialize Azure provider with API key and version.

        Args:
            base_url: Azure OpenAI endpoint base URL
            api_key: Azure OpenAI API key for authentication
            api_version: Azure OpenAI API version (e.g., "2024-02-15-preview")
            provider_fee: Provider fee multiplier (default 1.01 for 1% fee)
        """
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )
        self.api_version = api_version

    @classmethod
    def from_db_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "AzureUpstreamProvider | None":
        if not provider_row.api_version:
            return None
        return cls(
            base_url=provider_row.base_url,
            api_key=provider_row.api_key,
            api_version=provider_row.api_version,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Azure OpenAI",
            "default_base_url": "",
            "fixed_base_url": False,
            "platform_url": cls.platform_url,
        }

    def prepare_params(
        self, path: str, query_params: Mapping[str, str] | None
    ) -> Mapping[str, str]:
        """Prepare query parameters for Azure OpenAI, adding API version.

        Args:
            path: Request path
            query_params: Original query parameters from the client

        Returns:
            Query parameters dict with Azure API version added for chat completions
        """
        params = dict(query_params or {})
        if path.endswith("chat/completions"):
            params["api-version"] = self.api_version
        return params
