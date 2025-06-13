import asyncio
import json
import os
from pathlib import Path
from pydantic.v1 import BaseModel

from .price import sats_usd_ask_price


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
    description: str
    context_length: int
    architecture: Architecture
    pricing: Pricing
    sats_pricing: Pricing | None = None
    per_request_limits: dict | None = None
    top_provider: TopProvider | None = None


MODELS: list[Model] = []


def load_models() -> list[Model]:
    """Load model definitions from a JSON file.

    The file path can be specified via the ``MODELS_PATH`` environment variable.
    If ``models.json`` is not found, the bundled ``models.example.json`` is used
    as a fallback. If neither file exists or an error occurs while loading, an
    empty list is returned.
    """

    models_path = Path(os.environ.get("MODELS_PATH", "models.json"))
    if not models_path.exists():
        example = Path(__file__).resolve().parent.parent / "models.example.json"
        if example.exists():
            models_path = example
        else:
            return []

    try:
        with models_path.open("r") as f:
            data = json.load(f)
    except Exception as e:  # pragma: no cover - log and continue
        print(f"Error loading models from {models_path}: {e}")
        return []

    return [Model(**model) for model in data.get("models", [])]


MODELS = load_models()


async def update_sats_pricing() -> None:
    while True:
        try:
            sats_to_usd = await sats_usd_ask_price()
            for model in MODELS:
                model.sats_pricing = Pricing(
                    **{k: v / sats_to_usd for k, v in model.pricing.dict().items()}
                )
                if model.top_provider:
                    if (
                        model.top_provider.context_length
                        and model.top_provider.max_completion_tokens
                    ):
                        max_context_cost = (
                            model.top_provider.context_length
                            * model.sats_pricing.prompt
                        )
                        max_completion_cost = (
                            model.top_provider.max_completion_tokens
                            * model.sats_pricing.completion
                        )
                        model.sats_pricing.max_cost = (
                            max_context_cost + max_completion_cost
                        )
                    elif model.top_provider.context_length:
                        max_context_cost = (
                            model.top_provider.context_length
                            * model.sats_pricing.prompt
                        )
                        max_completion_cost = 32_000 * model.sats_pricing.completion
                        model.sats_pricing.max_cost = (
                            max_context_cost + max_completion_cost
                        )
                    elif model.top_provider.max_completion_tokens:
                        max_completion_cost = (
                            model.top_provider.max_completion_tokens
                            * model.sats_pricing.completion
                        )
                        max_context_cost = 1_048_576 * model.sats_pricing.prompt
                        model.sats_pricing.max_cost = max_completion_cost
                    else:
                        model.sats_pricing.max_cost = (
                            1_048_576 * model.sats_pricing.prompt
                            + 32_000 * model.sats_pricing.completion
                        )
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
            print('Error updating sats pricing: ', e)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            break
