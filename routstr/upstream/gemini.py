from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import gemini_messages
from .base import BaseUpstreamProvider
from .clients.gemini import GeminiClient

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow
    from ..payment.models import Model

from ..core.logging import get_logger

logger = get_logger(__name__)


class GeminiUpstreamProvider(BaseUpstreamProvider):
    """Gemini provider — proxies through Gemini's OpenAI-compat surface.

    The chat-completions, embeddings, and models paths all flow through
    :meth:`BaseUpstreamProvider.forward_request`; we only override
    ``get_request_base_url`` to redirect to ``{base}/openai/...`` and
    ``_dispatch_anthropic_messages`` to inject thought-signatures on the
    /v1/messages path (see :mod:`gemini_messages` for that rationale).
    """

    provider_type = "gemini"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    platform_url = "https://aistudio.google.com/app/apikey"
    litellm_provider_prefix = "gemini/"

    def __init__(
        self,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: str = "",
        provider_fee: float = 1.01,
    ):
        super().__init__(
            api_key=api_key,
            provider_fee=provider_fee,
            base_url=base_url,
        )
        self._client: GeminiClient | None = None

    @property
    def client(self) -> GeminiClient:
        """Get or create the Gemini API client (used for the models listing)."""
        if self._client is None:
            self._client = GeminiClient(api_key=self.api_key)
        return self._client

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "GeminiUpstreamProvider":
        return cls(
            base_url=provider_row.base_url,
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Google Gemini",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

    def transform_model_name(self, model_id: str) -> str:
        """Reduce a routstr model id to the bare upstream Gemini name.

        Gemini's OpenAI-compat surface expects the literal model id
        (e.g. ``gemini-3.1-flash-lite-preview``) — no ``gemini/`` provider
        prefix and no ``google/`` vendor sub-prefix. Take the last path
        segment so we tolerate any of:

            ``gemini-2.0-flash``
            ``gemini/gemini-2.0-flash``
            ``gemini/google/gemini-3.1-flash-lite-preview``
        """
        return model_id.rsplit("/", 1)[-1]

    @property
    def compat_base_url(self) -> str:
        """Gemini's OpenAI-compat surface, regardless of what's stored.

        Stored ``base_url`` may be ``.../v1beta`` (the native Gemini API
        root) or ``.../v1beta/openai`` (already pointed at the compat
        surface). Normalize to the latter.
        """
        return self.base_url.rstrip("/").removesuffix("/openai") + "/openai"

    def get_request_base_url(
        self, path: str, model_obj: "Model | None" = None
    ) -> str:
        """Route every proxied request to the OpenAI-compat surface.

        Required because the stored ``base_url`` typically points at the
        native Gemini API (``/v1beta``), but :meth:`forward_request`
        forwards OpenAI-shaped paths (``/chat/completions``,
        ``/embeddings``, ``/models``) which only exist under the
        ``/openai`` subtree.
        """
        return self.compat_base_url

    async def _dispatch_anthropic_messages(
        self,
        request_body: bytes | None,
        model_obj: "Model",
        *,
        log_extra: dict[str, Any] | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Dispatch /v1/messages via the gemini-specific httpx path.

        See :mod:`routstr.upstream.gemini_messages` for the full rationale
        (thought-signature injection, why litellm + the openai SDK can't
        carry the required ``extra_content`` field).
        """
        return await gemini_messages.dispatch_gemini_messages(
            request_body=request_body,
            model_obj=model_obj,
            base_url=self.compat_base_url,
            api_key=self.api_key,
            transform_model_name=self.transform_model_name,
            log_extra=log_extra,
        )

    async def _fetch_provider_models(self) -> dict:
        """Fetch models from Gemini API via the OpenAI-compat client."""
        try:
            models_data = await self.client.list_models()

            for model in models_data:
                if "id" in model and model["id"].startswith("models/"):
                    model["id"] = model["id"].removeprefix("models/")

            return {"data": models_data}
        except Exception as e:
            logger.error(
                f"Failed to fetch models from Gemini API: {e}",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "base_url": self.base_url,
                },
            )
            return {"data": []}
