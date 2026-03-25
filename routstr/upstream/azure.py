from typing import TYPE_CHECKING, Mapping

from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow
    from ..payment.models import Model


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

    def prepare_headers(self, request_headers: dict) -> dict:
        """Prepare headers for Azure OpenAI, adding api-key."""
        headers = super().prepare_headers(request_headers)
        if self.api_key:
            headers["api-key"] = self.api_key
            headers.pop("Authorization", None)
            headers.pop("authorization", None)
        return headers

    def prepare_params(
        self, path: str, query_params: Mapping[str, str] | None
    ) -> Mapping[str, str]:
        """Prepare query parameters for Azure OpenAI, adding API version."""
        params = dict(query_params or {})
        version = (self.api_version or "").replace("\ufeff", "").strip()
        if not version or version.lower() == "v1":
            version = "2024-02-15-preview"
        params["api-version"] = version
        return params

    def normalize_request_path(
        self, path: str, model_obj: "Model | None" = None
    ) -> str:
        """Build Azure deployment-specific request path."""
        clean_path = super().normalize_request_path(path, model_obj).lstrip("/")
        if model_obj is None:
            return clean_path

        deployment_id = getattr(
            model_obj, "canonical_slug", None
        ) or self.transform_model_name(model_obj.id)
        deployment_id = deployment_id.split("/")[-1]
        return f"openai/deployments/{deployment_id}/{clean_path}"

    def get_request_base_url(
        self, path: str, model_obj: "Model | None" = None
    ) -> str:
        """Use endpoint root, stripping accidental /openai/v1 suffix if present."""
        base_url = self.base_url.rstrip("/")
        marker = "/openai/v1"
        if marker in base_url:
            base_url = base_url.split(marker, 1)[0].rstrip("/")
        return base_url

    def transform_model_name(self, model_id: str) -> str:
        """Extract deployment name from model ID."""
        if "/" in model_id:
            return model_id.split("/")[-1]
        return model_id
