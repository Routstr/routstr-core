import json
import re
from typing import TYPE_CHECKING

import httpx

from ..core.exceptions import UpstreamError
from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

# Body fields the API accepts only with their beta. Relays such as routstrd
# drop client headers, so the beta is restored from the body that needs it.
_FIELD_BETAS = (
    ("context_management", "context-management-2025-06-27"),
    ("safeguards", "dangerous-tool-use-2026-09-03"),
)
_MESSAGE_FIELD_BETAS = (("output_config", "per-turn-control-2026-07-01"),)

_NATIVE_CLAUDE_ID = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?(?:-\d{8})?$"
)


def _rejects_budgeted_thinking(native_id: object) -> bool:
    """Whether Anthropic answers ``thinking.type: enabled`` with a 400.

    Probed against the API: Opus 4.7+, every 5.x model and Fable accept only
    adaptive thinking; 4.6 accepts both and 4.5 accepts only budgets.
    """
    if not isinstance(native_id, str):
        return False
    match = _NATIVE_CLAUDE_ID.match(native_id)
    if match is None:
        return False
    if match["family"] == "fable":
        return True
    return (int(match["major"]), int(match["minor"] or 0)) >= (4, 7)


def _effort_for_budget(budget: int) -> str:
    # Tiers bracket the common client budgets (2k/8k/16k for low/medium/high).
    if budget <= 4096:
        return "low"
    if budget <= 12288:
        return "medium"
    return "high"


def _normalize_thinking(data: dict) -> bool:
    """Rewrite client thinking shapes the native API rejects.

    Clients that predate adaptive-only models still send budgeted thinking,
    and Claude Code may ask for the ``highlights`` display, which the API only
    serves to Anthropic-hosted sessions. Other providers (OpenRouter, the
    litellm path) already translate both, so without this the same request
    succeeds or fails depending on which provider the router picks.
    """
    thinking = data.get("thinking")
    if not isinstance(thinking, dict):
        return False
    normalized = dict(thinking)
    if normalized.get("display") == "highlights":
        # Same fallback Claude Code applies itself once the API rejects it.
        normalized["display"] = "omitted"
    if normalized.get("type") == "enabled" and _rejects_budgeted_thinking(
        data.get("model")
    ):
        budget = normalized.pop("budget_tokens", None)
        normalized["type"] = "adaptive"
        config = data.get("output_config", {})
        if (
            isinstance(budget, int)
            and isinstance(config, dict)
            and "effort" not in config
        ):
            data["output_config"] = {**config, "effort": _effort_for_budget(budget)}
    if normalized == thinking:
        return False
    data["thinking"] = normalized
    return True


class AnthropicUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for Anthropic API."""

    provider_type = "anthropic"
    default_base_url = "https://api.anthropic.com/v1"
    platform_url = "https://console.anthropic.com/settings/keys"
    supports_anthropic_messages = True
    litellm_provider_prefix = "anthropic/"

    def __init__(
        self,
        api_key: str,
        provider_fee: float = 1.01,
        workspace_id: str | None = None,
    ):
        super().__init__(
            base_url=self.default_base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )
        self.workspace_id = workspace_id.strip() if workspace_id else None
        self._native_model_names: dict[str, str] = {}

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "AnthropicUpstreamProvider":
        try:
            settings = json.loads(provider_row.provider_settings or "{}")
        except (TypeError, ValueError):
            settings = {}
        workspace_id = (
            settings.get("workspace_id") if isinstance(settings, dict) else None
        )
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
            workspace_id=workspace_id if isinstance(workspace_id, str) else None,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Anthropic",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'anthropic/' prefix for Anthropic API compatibility and transform model names."""
        if model_id.startswith("anthropic/"):
            model_id = model_id[len("anthropic/") :]
        if model_id in self._native_model_names:
            return self._native_model_names[model_id]
        if model_id in self._native_model_names.values():
            return model_id
        fixed_transforms = {
            "claude-haiku-4.5": "claude-haiku-4-5-20251001",
            "claude-sonnet-4.5": "claude-sonnet-4-5-20250929",
            "claude-opus-4.1": "claude-opus-4-1-20250805",
            "claude-opus-4": "claude-opus-4-20250514",
            "claude-sonnet-4": "claude-sonnet-4-20250514",
            "claude-3.5-haiku": "claude-3-5-haiku-20241022",
            "claude-3-haiku": "claude-3-haiku-20240307",
            "claude-haiku-4-5": "claude-haiku-4-5-20251001",
            "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
            "claude-opus-4-1": "claude-opus-4-1-20250805",
            "claude-3-5-haiku": "claude-3-5-haiku-20241022",
        }
        if model_id in fixed_transforms:
            model_id = fixed_transforms[model_id]
        return model_id

    def prepare_headers(self, request_headers: dict) -> dict:
        headers = super().prepare_headers(request_headers)
        # Match Anthropic's SDK default API version while preserving client overrides.
        if not any(name.lower() == "anthropic-version" for name in headers):
            headers["anthropic-version"] = "2023-06-01"
        if self.workspace_id:
            # The configured upstream workspace takes precedence over client headers.
            headers = {
                name: value
                for name, value in headers.items()
                if name.lower() != "anthropic-workspace-id"
            }
            headers["anthropic-workspace-id"] = self.workspace_id
        return headers

    def prepare_body_headers(self, headers: dict, body: bytes | None) -> dict:
        if not body:
            return headers
        try:
            data = json.loads(body)
        except ValueError:
            return headers
        if not isinstance(data, dict):
            return headers
        required = [beta for field, beta in _FIELD_BETAS if field in data]
        messages = data.get("messages")
        if isinstance(messages, list):
            required += [
                beta
                for field, beta in _MESSAGE_FIELD_BETAS
                if any(isinstance(m, dict) and field in m for m in messages)
            ]
        if not required:
            return headers
        name = next(
            (key for key in headers if key.lower() == "anthropic-beta"),
            "anthropic-beta",
        )
        present = [b.strip() for b in headers.get(name, "").split(",") if b.strip()]
        missing = [beta for beta in required if beta not in present]
        if not missing:
            return headers
        return {**headers, name: ",".join(present + missing)}

    def _apply_reasoning_effort(self, data: dict, model_obj: Model) -> bool:
        # Generic effort is resolved first so it wins over a translated budget.
        changed = self._apply_generic_effort(data)
        return _normalize_thinking(data) or changed

    def _apply_generic_effort(self, data: dict) -> bool:
        # Anthropic defaults are independent of OpenRouter's mandatory metadata.
        if "reasoning_effort" not in data and "reasoning" not in data:
            return False

        reasoning = data.get("reasoning")
        if reasoning is None:
            reasoning = {}
        if not isinstance(reasoning, dict) or set(reasoning) - {"effort"}:
            raise UpstreamError(
                "For Anthropic, reasoning must contain only effort",
                status_code=400,
            )
        efforts: list[str] = []
        for value in (data.get("reasoning_effort"), reasoning.get("effort")):
            if value is None:
                continue
            if not isinstance(value, str) or value not in {
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            }:
                raise UpstreamError("Unsupported Anthropic effort", status_code=400)
            efforts.append(value)

        if efforts:
            config = data.get("output_config", {})
            if not isinstance(config, dict):
                raise UpstreamError(
                    "Anthropic output_config must be an object", status_code=400
                )
            native_effort = config.get("effort")
            if any(value != efforts[0] for value in efforts) or (
                native_effort is not None and native_effort != efforts[0]
            ):
                raise UpstreamError("Conflicting Anthropic effort", status_code=400)
            data["output_config"] = {**config, "effort": efforts[0]}

        data.pop("reasoning_effort", None)
        data.pop("reasoning", None)
        return True

    async def _fetch_native_model_ids(self) -> set[str]:
        native_ids: set[str] = set()
        params: dict[str, str | int] = {"limit": 1000}
        seen_cursors: set[str] = set()
        async with httpx.AsyncClient(timeout=20.0) as client:
            while True:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers=self.prepare_headers({}),
                    params=params,
                )
                response.raise_for_status()
                page = response.json()
                data = page["data"]
                if not isinstance(data, list):
                    raise ValueError("Invalid Anthropic models response")
                native_ids.update(model["id"] for model in data)
                if not page.get("has_more"):
                    return native_ids
                cursor = page.get("last_id")
                if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                    raise ValueError("Invalid Anthropic models pagination cursor")
                seen_cursors.add(cursor)
                params["after_id"] = cursor

    async def fetch_models(self) -> list[Model]:
        # Native IDs are authoritative; OpenRouter supplies pricing, not API names.
        # Models keep their catalog ID so saved overrides stay keyed as before;
        # the native ID is only an alias and the name sent upstream.
        # A discovery failure raises so refresh_models_cache retains its last cache.
        native_ids = await self._fetch_native_model_ids()
        models_data = await async_fetch_openrouter_models(source_filter="anthropic")
        models: list[Model] = []
        mappings: dict[str, str] = {}
        for data in models_data:
            model = Model(**data)
            candidates = (
                self.transform_model_name(model.id),
                model.id,
                model.id.replace(".", "-"),
            )
            native_id = next((name for name in candidates if name in native_ids), None)
            if native_id is None:
                continue
            mappings[model.id] = native_id
            model.alias_ids = [native_id]
            models.append(model)
        self._native_model_names = mappings
        return models
