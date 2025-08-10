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
    canonical_slug: str | None = None
    provider: str | None
    created: int
    description: str
    context_length: int
    architecture: Architecture
    pricing: Pricing
    sats_pricing: Pricing | None = None
    per_request_limits: dict | None = None
    top_provider: TopProvider | None = None


MODELS: list[Model] = []


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


def load_models() -> list[Model]:
    """Load model definitions from a JSON file or auto-generate from OpenRouter API.

    The file path can be specified via the ``MODELS_PATH`` environment variable.
    If a user-provided models.json exists, it will be used. Otherwise, models are
    automatically fetched from OpenRouter API in memory. If the example file exists
    and no user file is provided, it will be used as a fallback.
    """

    models_path = Path(os.environ.get("MODELS_PATH", "models.json"))

    # Check if user has actively provided a models.json file
    if models_path.exists():
        print(f"Loading models from user-provided file: {models_path}")
        try:
            with models_path.open("r") as f:
                data = json.load(f)
            return [Model(**model) for model in data.get("models", [])]
        except Exception as e:
            print(f"Error loading models from {models_path}: {e}")
            # Fall through to auto-generation

    # Auto-generate models from OpenRouter API
    print("Auto-generating models from OpenRouter API")
    source_filter = os.getenv("SOURCE")
    source_filter = source_filter if source_filter and source_filter.strip() else None

    models_data = fetch_openrouter_models(source_filter=source_filter)
    if not models_data:
        print("Failed to fetch models from OpenRouter API")
        return []

    print(f"Successfully fetched {len(models_data)} models from OpenRouter API")
    return [Model(**model) for model in models_data]


MODELS = load_models()


async def update_sats_pricing() -> None:
    while True:
        try:
            sats_to_usd = await sats_usd_ask_price()
            for model in MODELS:
                model.sats_pricing = Pricing(
                    **{k: v / sats_to_usd for k, v in model.pricing.dict().items()}
                )
                mspp = model.sats_pricing.prompt
                mspc = model.sats_pricing.completion
                if (tp := model.top_provider) and (
                    tp.context_length or tp.max_completion_tokens
                ):
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
        except asyncio.CancelledError:
            break
        except Exception as e:
            print("Error updating sats pricing: ", e)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            break


def match_model_id_to_internal_model(model_id: str) -> Model | None:
    """Match a model ID to an internal model."""
    if not model_id:
        return None

    if "/" in model_id:
        _, model_id = model_id.split("/")

    for model in MODELS:
        if model.id == model_id:
            return model
        if model.canonical_slug == model_id:
            return model
        if model_id in model.id:
            return model

    return None


@models_router.get("/v1/models")
@models_router.get("/models")
async def models() -> dict:
    return {"data": MODELS}
