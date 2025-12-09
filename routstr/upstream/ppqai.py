from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel

from ..core.logging import get_logger
from ..payment.models import Architecture, Model, Pricing, async_fetch_openrouter_models
from .base import BaseUpstreamProvider, TopupData

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)


class PPQAIModelPricing(BaseModel):
    ui: dict[str, float]
    api: dict[str, float]


class PPQAIModel(BaseModel):
    id: str
    provider: str
    name: str
    created_at: int
    context_length: int
    pricing: PPQAIModelPricing
    popular: bool


class PPQAIUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider for PPQ.AI API with Lightning Network top-up support."""

    provider_type = "ppqai"
    default_base_url = "https://api.ppq.ai"
    platform_url = "https://ppq.ai/api-docs"

    def __init__(self, api_key: str, provider_fee: float = 1.0):
        super().__init__(
            base_url=self.default_base_url, api_key=api_key, provider_fee=provider_fee
        )

    @classmethod
    def from_db_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "PPQAIUpstreamProvider":
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "PPQ.AI",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
            "can_create_account": True,
            "can_topup": True,
            "can_show_balance": True,
        }

    def transform_model_name(self, model_id: str) -> str:
        return model_id

    @classmethod
    async def create_account_static(cls) -> dict[str, object]:
        """Create a new PPQ.AI account without requiring an instance.

        Returns:
            Dict containing 'credit_id' and 'api_key' for the new account.

        Raises:
            httpx.HTTPStatusError: If the API request fails.
        """
        url = f"{cls.default_base_url}/accounts/create"

        logger.info("Creating new PPQ.AI account", extra={"url": url})

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url)
            response.raise_for_status()
            account_data = response.json()

            logger.info(
                "Successfully created PPQ.AI account",
                extra={
                    "credit_id": account_data.get("credit_id"),
                    "has_api_key": bool(account_data.get("api_key")),
                },
            )

            return account_data

    async def fetch_models(self) -> list[Model]:
        """Fetch models from PPQ.AI API."""
        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        logger.debug(
            "Fetching models from PPQ.AI",
            extra={"url": url, "has_api_key": bool(self.api_key)},
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()

                models_data = data.get("data", [])
                logger.info(
                    "Fetched models from PPQ.AI",
                    extra={"model_count": len(models_data)},
                )

                or_models = [
                    Model(**model)  # type: ignore
                    for model in await async_fetch_openrouter_models()
                ]

                models = []
                for model_data in models_data:
                    try:
                        ppqai_model = PPQAIModel.parse_obj(model_data)
                        or_model = next(
                            (
                                model
                                for model in or_models
                                if (model.id == ppqai_model.id)
                                or (model.id.split("/")[-1] == ppqai_model.id)
                                or (model.id == ppqai_model.id.split("/")[-1])
                            ),
                            None,
                        )

                        if or_model:
                            if input_price := ppqai_model.pricing.api.get(
                                "input_per_1M"
                            ):
                                or_model.pricing.prompt = input_price / 1_000_000
                            if output_price := ppqai_model.pricing.api.get(
                                "output_per_1M"
                            ):
                                or_model.pricing.completion = output_price / 1_000_000
                            if cl := ppqai_model.context_length:
                                or_model.context_length = cl
                            models.append(or_model)
                        else:
                            input_price = ppqai_model.pricing.api.get(
                                "input_per_1M", 0.0
                            )
                            output_price = ppqai_model.pricing.api.get(
                                "output_per_1M", 0.0
                            )

                            models.append(
                                Model(
                                    id=ppqai_model.id,
                                    name=ppqai_model.name,
                                    created=ppqai_model.created_at // 1000,
                                    description=f"{ppqai_model.provider} model",
                                    context_length=ppqai_model.context_length,
                                    architecture=Architecture(
                                        modality="text->text",
                                        input_modalities=["text"],
                                        output_modalities=["text"],
                                        tokenizer="Unknown",
                                        instruct_type=None,
                                    ),
                                    pricing=Pricing(
                                        prompt=input_price / 1_000_000,
                                        completion=output_price / 1_000_000,
                                        request=0.0,
                                        image=0.0,
                                        web_search=0.0,
                                        internal_reasoning=0.0,
                                    ),
                                )
                            )
                    except Exception as e:
                        logger.warning(
                            "Failed to parse PPQ.AI model",
                            extra={
                                "model_id": model_data.get("id", "unknown"),
                                "error": str(e),
                                "error_type": type(e).__name__,
                            },
                        )

                return models

        except httpx.HTTPStatusError as e:
            logger.error(
                "HTTP error fetching models from PPQ.AI",
                extra={
                    "status_code": e.response.status_code,
                    "error": str(e),
                },
            )
            return []
        except Exception as e:
            logger.error(
                "Error fetching models from PPQ.AI",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            return []

    async def create_account(self) -> dict[str, object]:
        """Create a new PPQ.AI account.

        Returns:
            Dict containing 'credit_id' and 'api_key' for the new account.

        Raises:
            httpx.HTTPStatusError: If the API request fails.
        """
        url = f"{self.base_url}/accounts/create"

        logger.info("Creating new PPQ.AI account", extra={"url": url})

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url)
            response.raise_for_status()
            account_data = response.json()

            logger.info(
                "Successfully created PPQ.AI account",
                extra={
                    "credit_id": account_data.get("credit_id"),
                    "has_api_key": bool(account_data.get("api_key")),
                },
            )

            return account_data

    async def create_lightning_topup(
        self, amount: int, currency: str
    ) -> dict[str, object]:
        """Create a Lightning Network top-up invoice for this account.

        Args:
            amount: Amount to top up (in the specified currency)
            currency: Currency for the top-up (default: "USD")

        Returns:
            Dict containing invoice details including 'invoice_id', 'payment_request', etc.

        Raises:
            httpx.HTTPStatusError: If the API request fails.
        """
        url = f"{self.base_url}/topup/create/btc-lightning"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"amount": amount, "currency": currency}

        logger.info(
            "Creating Lightning top-up invoice",
            extra={"url": url, "amount": amount, "currency": currency},
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            print(f"Payload: {payload}", "sending to", url)
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            invoice_data = response.json()

            logger.info(
                "Successfully created Lightning top-up invoice",
                extra={
                    "invoice_id": invoice_data.get("invoice_id"),
                    "amount": amount,
                    "currency": currency,
                },
            )

            return invoice_data

    async def check_topup_status(self, invoice_id: str) -> bool:
        """Check the status of a Lightning top-up invoice.

        Args:
            invoice_id: The invoice ID to check

        Returns:
            True if the invoice is paid (status == "Settled"), False otherwise

        Raises:
            httpx.HTTPStatusError: If the API request fails.
        """
        url = f"{self.base_url}/topup/status/{invoice_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        logger.debug(
            "Checking Lightning top-up status",
            extra={"url": url, "invoice_id": invoice_id},
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            status_data = response.json()

            is_paid = status_data.get("status") == "Settled"

            logger.debug(
                "Retrieved Lightning top-up status",
                extra={
                    "invoice_id": invoice_id,
                    "status": status_data.get("status"),
                    "is_paid": is_paid,
                },
            )

            return is_paid

    async def initiate_topup(self, amount: int) -> TopupData:
        """Initiate a Lightning Network top-up for the PPQ.AI account.

        Args:
            amount: Amount in currency units to top up (will be sent to PPQ.AI API)

        Returns:
            TopupData with standardized invoice information

        Raises:
            httpx.HTTPStatusError: If the API request fails
        """
        ppq_response = await self.create_lightning_topup(amount, "USD")

        logger.info(
            "PPQ.AI top-up response",
            extra={
                "ppq_response": ppq_response,
                "invoice_id": ppq_response.get("invoice_id"),
                "has_lightning_invoice": "lightning_invoice" in ppq_response,
            },
        )

        expires_at_value = ppq_response.get("expires_at")
        checkout_url_value = ppq_response.get("checkout_url")

        topup_data = TopupData(
            invoice_id=str(ppq_response["invoice_id"]),
            payment_request=str(ppq_response["lightning_invoice"]),
            amount=int(ppq_response["amount"])
            if isinstance(ppq_response["amount"], (int, float, str))
            else 0,
            currency=str(ppq_response["currency"]),
            expires_at=int(expires_at_value)
            if isinstance(expires_at_value, (int, float, str))
            and expires_at_value is not None
            else None,
            checkout_url=str(checkout_url_value)
            if checkout_url_value is not None
            else None,
        )

        logger.info(
            "Created TopupData",
            extra={
                "invoice_id": topup_data.invoice_id,
                "payment_request_length": len(topup_data.payment_request),
                "amount": topup_data.amount,
            },
        )

        return topup_data

    async def get_balance(self) -> float | None:
        """Get the current account balance from PPQ.AI.

        Returns:
            Float representing the balance amount (in USD), or None if unavailable.

        Raises:
            httpx.HTTPStatusError: If the API request fails
        """
        data = await self.check_balance()
        balance = data.get("balance")
        if isinstance(balance, (int, float)):
            return float(balance)
        return None

    async def check_balance(self) -> dict[str, object]:
        """Check the account balance for this PPQ.AI account.

        Returns:
            Dict containing balance information

        Raises:
            httpx.HTTPStatusError: If the API request fails.
        """
        url = f"{self.base_url}/credits/balance"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        logger.debug("Checking PPQ.AI account balance", extra={"url": url})

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json={})
            response.raise_for_status()
            balance_data = response.json()

            logger.debug(
                "Retrieved PPQ.AI account balance",
                extra={"balance": balance_data.get("balance")},
            )

            return balance_data
