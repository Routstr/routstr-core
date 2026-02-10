from typing import TYPE_CHECKING, Mapping

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..auth import ApiKey
    from ..core.db import AsyncSession, UpstreamProviderRow
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
        # Ensure we use a valid Azure API version format
        # Strip any hidden characters like Byte Order Marks (BOM) or whitespace
        version = self.api_version.strip().replace("\ufeff", "")
        if version == "v1":
            version = "2024-02-15-preview"
        params["api-version"] = version
        return params

    async def forward_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        request_body: bytes | None,
        key: "ApiKey",
        max_cost_for_model: int,
        session: "AsyncSession",
        model_obj: "Model",
    ) -> Response | StreamingResponse:
        """Forward request to Azure OpenAI."""
        # Fix: If base_url contains /openai/v1, remove it
        actual_base_url = self.base_url
        if "/openai/v1" in actual_base_url:
            actual_base_url = actual_base_url.split("/openai/v1")[0]

        # Use canonical_slug as it often stores the deployment name in Azure setups
        # otherwise fallback to transform_model_name
        deployment_id = getattr(
            model_obj, "canonical_slug", None
        ) or self.transform_model_name(model_obj.id)

        # Ensure deployment_id doesn't contain a provider prefix (e.g., 'openai/' or 'azure/')
        if "/" in deployment_id:
            deployment_id = deployment_id.split("/")[-1]

        # Azure format: openai/deployments/{deployment-id}/chat/completions
        clean_path = path.lstrip("/")
        if clean_path.startswith("v1/"):
            clean_path = clean_path[3:]
        azure_path = f"openai/deployments/{deployment_id}/{clean_path}"

        # Temporary backup and restore base_url to use cleaned version
        original_base = self.base_url
        self.base_url = actual_base_url

        # The query params are handled by super().forward_request via prepare_params
        # We don't need to manually append them to full_url for the print if we want to be accurate
        params = self.prepare_params(path, {})
        full_url = (
            f"{actual_base_url}/{azure_path}?api-version={params.get('api-version')}"
        )
        print(f"\n[DEBUG] Azure Forwarding URL: {full_url}")
        print(f"[DEBUG] Deployment ID: {deployment_id}")

        try:
            response = await super().forward_request(
                request,
                azure_path,
                headers,
                request_body,
                key,
                max_cost_for_model,
                session,
                model_obj,
            )

            # Check if it's an error response to print details
            if hasattr(response, "status_code") and response.status_code != 200:
                print(f"[DEBUG] Azure Error Status: {response.status_code}")
                if hasattr(response, "body"):
                    print(
                        f"[DEBUG] Azure Error Body: {response.body.decode() if isinstance(response.body, bytes) else response.body}"
                    )

            return response
        except Exception as e:
            print(f"[DEBUG] Azure Exception: {str(e)}")
            raise
        finally:
            self.base_url = original_base

    def transform_model_name(self, model_id: str) -> str:
        """Extract deployment name from model ID."""
        if "/" in model_id:
            return model_id.split("/")[-1]
        return model_id
