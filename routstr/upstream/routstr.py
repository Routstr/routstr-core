from typing import TYPE_CHECKING, Any

import httpx

from ..core import get_logger
from ..payment.models import Model
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)


class RoutstrUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider for communicating with another Routstr instance."""

    provider_type = "routstr"
    default_base_url = None
    platform_url = None

    def __init__(
        self,
        base_url: str,
        api_key: str,
        provider_fee: float = 1.01,
        provider_settings: dict | None = None,
    ):
        """Initialize Routstr provider.

        Args:
            base_url: Base URL of the upstream Routstr instance
            api_key: API key for the upstream Routstr instance
            provider_fee: Provider fee multiplier
            provider_settings: Provider-specific settings (auto-topup, etc.)
        """
        # Ensure base_url doesn't end with /v1 as BaseUpstreamProvider appends it if needed
        # but Routstr paths are usually absolute from base.
        super().__init__(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            provider_fee=provider_fee,
        )
        self.settings = provider_settings or {}

    @classmethod
    def from_db_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "RoutstrUpstreamProvider":
        import json

        settings = {}
        if provider_row.provider_settings:
            try:
                settings = json.loads(provider_row.provider_settings)
            except Exception:
                pass

        return cls(
            base_url=provider_row.base_url,
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
            provider_settings=settings,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Routstr Node",
            "default_base_url": "",
            "fixed_base_url": False,
            "platform_url": cls.platform_url,
            "can_create_account": False,
            "can_topup": True,
            "can_show_balance": True,
        }

    async def get_balance(self) -> float | None:
        """Fetch balance from the upstream Routstr node.

        Returns:
            Balance in satoshis, or None if failed
        """
        url = f"{self.base_url}/v1/balance/info"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=headers, timeout=10.0)
                response.raise_for_status()
                data = response.json()
                # Routstr balance info usually contains 'balance' in msats or sats
                # Check for msats and convert to sats
                if "balance_msats" in data:
                    return float(data["balance_msats"]) / 1000.0
                return float(data.get("balance", 0))
            except Exception as e:
                logger.error(
                    "Failed to fetch balance from upstream Routstr",
                    extra={"url": url, "error": str(e)},
                )
                return None

    async def topup(self, cashu_token: str) -> dict[str, Any]:
        """Top up balance on the upstream Routstr node.

        Args:
            cashu_token: Cashu token to deposit

        Returns:
            Dict containing top-up result
        """
        url = f"{self.base_url}/v1/balance/topup"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {"cashu_token": cashu_token}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    url, headers=headers, json=payload, timeout=30.0
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.error(
                    "Failed to topup upstream Routstr",
                    extra={"url": url, "error": str(e)},
                )
                return {"error": str(e)}

    async def fetch_models(self) -> list[Model]:
        """Fetch models from the upstream Routstr node."""
        url = f"{self.base_url}/v1/models"

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers={}, timeout=15.0)
                response.raise_for_status()
                data = response.json()
                models = data.get("data", [])
                return [Model(**m) for m in models]
            except Exception as e:
                logger.error(
                    "Failed to fetch models from upstream Routstr",
                    extra={"url": url, "error": str(e)},
                )
                return []

    async def refund_balance(self) -> dict[str, Any]:
        """Request a refund from the upstream Routstr node.

        Returns:
            Dict containing refund result and token
        """
        url = f"{self.base_url}/v1/balance/refund"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers, timeout=30.0)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.error(
                    "Failed to request refund from upstream Routstr",
                    extra={"url": url, "error": str(e)},
                )
                return {"error": str(e)}
