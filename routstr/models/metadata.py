from typing import Final

import httpx

from ..core import get_logger

logger = get_logger(__name__)

DEFAULT_EXCLUDED_MODEL_IDS: Final[set[str]] = {
    "openrouter/auto",
    "google/gemini-2.5-pro-exp-03-25",
    "opengvlab/internvl3-78b",
    "openrouter/sonoma-dusk-alpha",
    "openrouter/sonoma-sky-alpha",
}


async def async_fetch_openrouter_models(source_filter: str | None = None) -> list[dict]:
    """Asynchronously fetch model information from OpenRouter API."""
    base_url = "https://openrouter.ai/api/v1"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/models", timeout=30)
            response.raise_for_status()
            data = response.json()

            models_data: list[dict] = []
            for model in data.get("data", []):
                model_id = model.get("id", "")

                if source_filter:
                    source_prefix = f"{source_filter}/"
                    if not model_id.startswith(source_prefix):
                        continue

                    model = dict(model)
                    model["id"] = model_id[len(source_prefix) :]
                    model_id = model["id"]

                if (
                    "(free)" in model.get("name", "")
                    or model_id in DEFAULT_EXCLUDED_MODEL_IDS
                ):
                    continue

                models_data.append(model)

            return models_data
    except Exception as e:
        logger.error(f"Error (async) fetching models from OpenRouter API: {e}")
        return []
