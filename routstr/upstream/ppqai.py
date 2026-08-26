from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

import httpx
from pydantic.v1 import BaseModel, Field

from ..core.logging import get_logger
from ..payment.models import Architecture, Model, Pricing, async_fetch_openrouter_models
from .base import BaseUpstreamProvider, TopupData
from .ehbp import EHBPForwardingTarget

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)

_PPQ_SAFE_READ_ATTEMPTS = 3
_PPQ_CIRCUIT_COOLDOWN_SECONDS = 30.0


class PPQCircuitOpenError(RuntimeError):
    pass


@dataclass
class _PPQCircuitState:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loop: asyncio.AbstractEventLoop | None = None


_ppq_circuits: dict[str, _PPQCircuitState] = {}


def _ppq_origin(url: str) -> str:
    parsed = httpx.URL(url)
    port = parsed.port or {"https": 443, "http": 80}.get(parsed.scheme, 0)
    return f"{parsed.scheme}://{parsed.host}:{port}"


async def _safe_read_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, object] | None = None,
) -> httpx.Response:
    state = _ppq_circuits.setdefault(_ppq_origin(url), _PPQCircuitState())
    loop = asyncio.get_running_loop()
    if state.loop is not loop:
        # Locks cannot be reused across event loops.
        state.lock = asyncio.Lock()
        state.loop = loop
    async with state.lock:
        remaining = state.cooldown_until - time.monotonic()
        if remaining > 0:
            raise PPQCircuitOpenError(
                f"PPQ.AI safe-read circuit is open; retry after {remaining:.2f}s"
            )

        for attempt in range(1, _PPQ_SAFE_READ_ATTEMPTS + 1):
            try:
                response = await client.request(method, url, headers=headers, json=json)
                response.raise_for_status()
                state.consecutive_failures = 0
                state.cooldown_until = 0.0
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                retryable_status = isinstance(error, httpx.HTTPStatusError) and (
                    error.response.status_code in {502, 503, 504}
                )
                if not isinstance(error, httpx.TransportError) and not retryable_status:
                    raise
                state.consecutive_failures += 1
                if attempt >= _PPQ_SAFE_READ_ATTEMPTS:
                    state.cooldown_until = (
                        time.monotonic() + _PPQ_CIRCUIT_COOLDOWN_SECONDS
                    )
                    raise
                base_delay = 0.25 * (2 ** (attempt - 1))
                delay = base_delay + random.uniform(0.0, base_delay)
                logger.warning(
                    "PPQ.AI safe read failed; retrying",
                    extra={
                        "url": url,
                        "attempt": attempt,
                        "max_attempts": _PPQ_SAFE_READ_ATTEMPTS,
                        "backoff_seconds": round(delay, 3),
                        "error": repr(error),
                        "error_type": type(error).__name__,
                    },
                )
                await asyncio.sleep(delay)
    raise RuntimeError("unreachable")


class PPQAIModelPricing(BaseModel):
    ui: Optional[dict[str, float]] = None
    api: Optional[dict[str, float]] = None
    input_per_1M_tokens: Optional[float] = Field(None, alias="input_per_1M_tokens")
    output_per_1M_tokens: Optional[float] = Field(None, alias="output_per_1M_tokens")


class PPQAIModel(BaseModel):
    id: str
    provider: Optional[str] = None
    name: str
    created_at: int
    context_length: int
    pricing: PPQAIModelPricing
    popular: bool = False


class PPQAIUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider for PPQ.AI API with Lightning Network top-up support."""

    provider_type = "ppqai"
    default_base_url = "https://api.ppq.ai"
    platform_url = "https://ppq.ai/api-docs"
    IGNORED_MODEL_IDS: list[str] = ["auto"]
    # PPQ.AI has a private encrypted endpoint, but this proxy currently has no
    # provider-attested usage extractor/model binding for it. Keep EHBP disabled
    # until a ConfidentialInferenceProfile can bill it without max-cost fallback.
    supports_ehbp = False

    def __init__(self, api_key: str, provider_fee: float = 1.0):
        super().__init__(
            base_url=self.default_base_url, api_key=api_key, provider_fee=provider_fee
        )

    @classmethod
    def _build_from_row(
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

    def get_ehbp_forwarding_target(
        self, path: str, model_obj: Model
    ) -> EHBPForwardingTarget:
        """Return the PPQ.AI private enclave target for EHBP requests.

        PPQ.AI exposes EHBP-aware inference under /private/v1/... separate
        from the public /v1/... endpoint. The encrypted body remains opaque to
        Routstr, so PPQ.AI also needs X-Private-Model for routing/billing.
        """
        return EHBPForwardingTarget(
            url=f"{self.base_url.rstrip('/')}/private/{path.lstrip('/')}",
            headers={"X-Private-Model": model_obj.forwarded_model_id or model_obj.id},
        )

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

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await _safe_read_request(client, "GET", url, headers=headers)
            data = response.json()

            models_data = data.get("data", [])

            or_models = [
                Model(**model)  # type: ignore
                for model in await async_fetch_openrouter_models()
            ]

            models = []
            for model_data in models_data:
                try:
                    ppqai_model = PPQAIModel.parse_obj(model_data)
                    if ppqai_model.id in self.IGNORED_MODEL_IDS:
                        continue

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
                        input_price = None
                        if ppqai_model.pricing.api:
                            input_price = ppqai_model.pricing.api.get("input_per_1M")
                        elif ppqai_model.pricing.input_per_1M_tokens:
                            input_price = ppqai_model.pricing.input_per_1M_tokens

                        if input_price is not None:
                            or_model.pricing.prompt = input_price / 1_000_000

                        output_price = None
                        if ppqai_model.pricing.api:
                            output_price = ppqai_model.pricing.api.get("output_per_1M")
                        elif ppqai_model.pricing.output_per_1M_tokens:
                            output_price = ppqai_model.pricing.output_per_1M_tokens

                        if output_price is not None:
                            or_model.pricing.completion = output_price / 1_000_000

                        if cl := ppqai_model.context_length:
                            or_model.context_length = cl
                        models.append(or_model)
                    else:
                        input_price = 0.0
                        if ppqai_model.pricing.api:
                            input_price = ppqai_model.pricing.api.get(
                                "input_per_1M", 0.0
                            )
                        elif ppqai_model.pricing.input_per_1M_tokens:
                            input_price = ppqai_model.pricing.input_per_1M_tokens

                        output_price = 0.0
                        if ppqai_model.pricing.api:
                            output_price = ppqai_model.pricing.api.get(
                                "output_per_1M", 0.0
                            )
                        elif ppqai_model.pricing.output_per_1M_tokens:
                            output_price = ppqai_model.pricing.output_per_1M_tokens

                        models.append(
                            Model(
                                id=ppqai_model.id,
                                name=ppqai_model.name,
                                created=ppqai_model.created_at // 1000,
                                description=f"{ppqai_model.provider or 'PPQ.AI'} model",
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

    async def on_upstream_error_redirect(
        self, status_code: int, error_message: str
    ) -> None:
        if "insufficient balance" in error_message.lower():
            logger.warning(
                f"Disabling PPQ.AI provider ({self.base_url}) due to insufficient balance",
                extra={"error": error_message},
            )
            from ..core.db import UpstreamProviderRow, create_session

            async with create_session() as session:
                provider = (
                    await session.get(UpstreamProviderRow, self.db_id)
                    if self.db_id is not None
                    else None
                )

                if provider:
                    provider.enabled = False
                    session.add(provider)
                    await session.commit()

                    # Trigger re-initialization of providers
                    # Import here to avoid circular dependency
                    from ..proxy import reinitialize_upstreams

                    await reinitialize_upstreams()

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
            response = await _safe_read_request(client, "GET", url, headers=headers)
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
        if isinstance(balance, (int, float)) and not isinstance(balance, bool):
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
            response = await _safe_read_request(
                client, "POST", url, headers=headers, json={}
            )
            balance_data = response.json()

            logger.debug(
                "Retrieved PPQ.AI account balance",
                extra={"balance": balance_data.get("balance")},
            )

            return balance_data
