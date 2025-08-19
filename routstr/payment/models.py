import asyncio
import json
import os
from pathlib import Path
from urllib.request import urlopen

from fastapi import APIRouter
from pydantic.v1 import BaseModel

from .price import sats_usd_ask_price

models_router = APIRouter()


class Architecture(BaseModel):
    modality: str
    input_modalities: list[str]
    output_modalities: list[str]
    tokenizer: str
    instruct_type: str | None


class Pricing(BaseModel):
    prompt: float
    completion: float
    request: float
    image: float
    web_search: float
    internal_reasoning: float
    max_cost: float = 0.0  # in sats not msats


class TopProvider(BaseModel):
    context_length: int | None = None
    max_completion_tokens: int | None = None
    is_moderated: bool | None = None


class Model(BaseModel):
    id: str
    name: str
    created: int
    description: str | None = None
    context_length: int | None = None
    architecture: Architecture
    pricing: Pricing
    sats_pricing: Pricing | None = None
    per_request_limits: dict | None = None
    top_provider: TopProvider | None = None
    provider_url: str | None = None


MODELS: list[Model] = []


def normalize_enhanced_model_data(model_data: dict) -> dict:
    """Normalize enhanced API model data to match Python Model class structure."""
    normalized = dict(model_data)

    # Ensure description is present (default to empty string if missing)
    if "description" not in normalized or normalized["description"] is None:
        normalized["description"] = ""

    # Ensure context_length is present (default to None if missing)
    if "context_length" not in normalized:
        normalized["context_length"] = None

    # Ensure name is present (fallback to id if missing)
    if "name" not in normalized or normalized["name"] is None:
        normalized["name"] = normalized.get("id", "Unknown Model")

    # Ensure architecture exists with minimal structure
    if "architecture" not in normalized or normalized["architecture"] is None:
        normalized["architecture"] = {
            "modality": "text->text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tokenizer": "Unknown",
            "instruct_type": None,
        }

    # Ensure pricing exists with minimal structure
    if "pricing" not in normalized or normalized["pricing"] is None:
        normalized["pricing"] = {
            "prompt": 0.0,
            "completion": 0.0,
            "request": 0.0,
            "image": 0.0,
            "web_search": 0.0,
            "internal_reasoning": 0.0,
            "max_cost": 0.0,
        }

    # Ensure created timestamp exists
    if "created" not in normalized or normalized["created"] is None:
        normalized["created"] = 0

    # Ensure provider_url is None if empty/whitespace (for proper fallback)
    if "provider_url" in normalized and isinstance(normalized["provider_url"], str):
        if not normalized["provider_url"].strip():
            normalized["provider_url"] = None

    return normalized


def fetch_enhanced_models(
    models_host: str, source_filter: str | None = None
) -> list[dict]:
    """Fetches enhanced model information from /api/enhanced-models endpoint."""
    base_url = models_host.rstrip("/")

    try:
        with urlopen(f"{base_url}/api/enhanced-models") as response:
            data = json.loads(response.read().decode("utf-8"))

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
                    or model_id == "openrouter/auto"
                    or model_id == "google/gemini-2.5-pro-exp-03-25"
                ):
                    continue

                models_data.append(model)

            return models_data
    except Exception as e:
        print(
            f"Error fetching enhanced models from {base_url}/api/enhanced-models: {e}"
        )
        return []


def fetch_openrouter_models(source_filter: str | None = None) -> list[dict]:
    """Fetches model information from OpenRouter API."""
    base_url = os.getenv("BASE_URL", "https://openrouter.ai/api/v1")

    try:
        with urlopen(f"{base_url}/models") as response:
            data = json.loads(response.read().decode("utf-8"))

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
                    or model_id == "openrouter/auto"
                    or model_id == "google/gemini-2.5-pro-exp-03-25"
                ):
                    continue

                models_data.append(model)

            return models_data
    except Exception as e:
        print(f"Error fetching models from OpenRouter API: {e}")
        return []


def load_models_from_enhanced_api(
    models_host: str, source_filter: str | None = None
) -> list[Model]:
    """Load models from enhanced API endpoint."""
    print(f"Loading models from enhanced API: {models_host}/api/enhanced-models")

    models_data = fetch_enhanced_models(models_host, source_filter=source_filter)
    if not models_data:
        print("Failed to fetch models from enhanced API")
        return []

    print(f"Successfully fetched {len(models_data)} models from enhanced API")

    models = []
    for model_data in models_data:
        try:
            # Ensure required fields have defaults if missing
            normalized_model = normalize_enhanced_model_data(model_data)
            model = Model(**normalized_model)
            models.append(model)
        except Exception as e:
            print(f"Error creating model from enhanced API data: {e}")
            print(f"Model data: {model_data}")
            continue

    return models


def load_models_from_openrouter_api(source_filter: str | None = None) -> list[Model]:
    """Load models from OpenRouter API."""
    print("Auto-generating models from OpenRouter API")

    models_data = fetch_openrouter_models(source_filter=source_filter)
    if not models_data:
        print("Failed to fetch models from OpenRouter API")
        return []

    print(f"Successfully fetched {len(models_data)} models from OpenRouter API")
    return [Model(**model) for model in models_data]


def load_models_from_file(models_path: Path) -> list[Model]:
    """Load models from JSON file."""
    print(f"Loading models from user-provided file: {models_path}")
    try:
        with models_path.open("r") as f:
            data = json.load(f)
        return [Model(**model) for model in data.get("models", [])]
    except Exception as e:
        print(f"Error loading models from {models_path}: {e}")
        return []


def load_models() -> list[Model]:
    """Load model definitions from a JSON file, enhanced API, or auto-generate from OpenRouter API.

    The file path can be specified via the ``MODELS_PATH`` environment variable.
    The enhanced API host can be specified via the ``MODELS_HOST`` environment variable.

    Priority order:
    1. If MODELS_PATH file exists, use it
    2. If MODELS_HOST is defined, fetch from enhanced API
    3. Otherwise, auto-generate from OpenRouter API
    """
    models_host = os.environ.get("MODELS_HOST")
    models_path = Path(os.environ.get("MODELS_PATH", "models.json"))
    source_filter = os.getenv("SOURCE")
    source_filter = source_filter if source_filter and source_filter.strip() else None

    # Check if MODELS_HOST is defined for enhanced API
    if models_host:
        models = load_models_from_enhanced_api(models_host, source_filter=source_filter)
        print(models)
        if models:
            return models
        # Fall through to OpenRouter API if enhanced API failed

    # Check if user has actively provided a models.json file
    if models_path.exists():
        models = load_models_from_file(models_path)
        if models:
            return models
        # Fall through to other methods if file loading failed

    # Fall back to OpenRouter API
    return load_models_from_openrouter_api(source_filter=source_filter)


MODELS = load_models()


def calculate_sats_pricing_for_model(model: Model, sats_to_usd: float) -> None:
    """Calculate sats pricing for a single model."""
    model.sats_pricing = Pricing(
        **{k: v / sats_to_usd for k, v in model.pricing.dict().items()}
    )
    mspp = model.sats_pricing.prompt
    mspc = model.sats_pricing.completion

    if (tp := model.top_provider) and (tp.context_length or tp.max_completion_tokens):
        if (cl := model.top_provider.context_length) and (
            mct := model.top_provider.max_completion_tokens
        ):
            model.sats_pricing.max_cost = (cl - mct) * mspp + mct * mspc
        elif cl := model.top_provider.context_length:
            model.sats_pricing.max_cost = cl * 0.8 * mspp + cl * 0.2 * mspc
        elif mct := model.top_provider.max_completion_tokens:
            model.sats_pricing.max_cost = mct * 4 * mspp + mct * mspc
        else:
            model.sats_pricing.max_cost = 1_000_000 * mspp + 32_000 * mspc
    elif model.context_length:
        model.sats_pricing.max_cost = (
            model.sats_pricing.prompt * model.context_length * 0.8
        ) + (model.sats_pricing.completion * model.context_length * 0.2)
    else:
        p = model.sats_pricing.prompt * 1_000_000
        c = model.sats_pricing.completion * 32_000
        r = model.sats_pricing.request * 100_000
        i = model.sats_pricing.image * 100
        w = model.sats_pricing.web_search * 1000
        ir = model.sats_pricing.internal_reasoning * 100
        model.sats_pricing.max_cost = p + c + r + i + w + ir


async def update_sats_pricing() -> None:
    """Update sats pricing for all models periodically."""
    while True:
        try:
            sats_to_usd = await sats_usd_ask_price()
            for model in MODELS:
                calculate_sats_pricing_for_model(model, sats_to_usd)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print("Error updating sats pricing: ", e)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            break


@models_router.get("/v1/models")
@models_router.get("/models")
async def models() -> dict:
    return {"data": MODELS}
