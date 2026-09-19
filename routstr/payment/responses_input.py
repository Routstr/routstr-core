"""Convert a Responses API ``input`` into chat ``messages`` via litellm.

litellm drops ``file_id`` (emits ``url: ""``) and nests a dict-form ``image_url``
as-is, so ``input_image`` parts are flattened to ``{image_url: str, detail}`` first.
``file_id`` becomes a sentinel URL the image walker treats as unfetchable.
"""

from typing import Any

from ..core import get_logger

logger = get_logger(__name__)


def __getattr__(name: str) -> Any:
    """Preserve the patchable config class without eagerly importing it."""
    if name == "LiteLLMCompletionResponsesConfig":
        from litellm.responses.litellm_completion_transformation.transformation import (
            LiteLLMCompletionResponsesConfig,
        )

        return LiteLLMCompletionResponsesConfig
    raise AttributeError(name)


FILE_ID_URL_PREFIX = "file-id:"


def _flatten_input_image(part: dict[str, Any]) -> tuple[str, str]:
    raw = part.get("image_url")
    url = raw.get("url", "") if isinstance(raw, dict) else raw
    detail = part.get("detail") or (
        raw.get("detail") if isinstance(raw, dict) else None
    )
    if not url and part.get("file_id"):
        url = f"{FILE_ID_URL_PREFIX}{part['file_id']}"
    return (url if isinstance(url, str) else ""), (detail or "auto")


def _normalize_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    if item.get("type") == "input_image":
        url, detail = _flatten_input_image(item)
        return {**item, "image_url": url, "detail": detail}
    content = item.get("content")
    if isinstance(content, list):
        return {**item, "content": [_normalize_item(part) for part in content]}
    return item


def input_image_part_to_image_url(part: dict[str, Any]) -> dict[str, Any]:
    """Reshape an ``input_image`` part found inside chat ``messages``."""
    url, detail = _flatten_input_image(part)
    return {"type": "image_url", "image_url": {"url": url, "detail": detail}}


def count_input_images(input_data: Any) -> int:
    if isinstance(input_data, dict):
        own = 1 if input_data.get("type") == "input_image" else 0
        return own + count_input_images(input_data.get("content"))
    if isinstance(input_data, list):
        return sum(count_input_images(item) for item in input_data)
    return 0


def responses_input_to_messages(input_data: Any) -> list[dict[str, Any]] | None:
    """Returns ``None`` when the transform fails so the caller can worst-case."""
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]
    if not isinstance(input_data, list):
        return []
    try:
        config = globals().get("LiteLLMCompletionResponsesConfig")
        if config is None:
            config = __getattr__("LiteLLMCompletionResponsesConfig")

        normalized = [_normalize_item(item) for item in input_data]
        converted = (
            config.transform_responses_api_input_to_messages(
                input=normalized,  # type: ignore[arg-type]
                responses_api_request={},
            )
        )
        return [dict(message) for message in converted]
    except Exception as e:
        logger.warning(
            "Responses input transform failed; using conservative image fallback",
            extra={"error": str(e)},
        )
        return None
