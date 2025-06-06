import asyncio
import json
import logging
from pydantic.v1 import BaseModel

from .price import sats_usd_ask_price

logger = logging.getLogger(__name__)


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

with open("models.json", "r") as f:
    MODELS = [Model(**model) for model in json.load(f)["models"]]


async def update_sats_pricing() -> None:
    while True:
        try:
            sats_to_usd = await sats_usd_ask_price()
            for model in MODELS:
                model.sats_pricing = Pricing(
                    **{k: v / sats_to_usd for k, v in model.pricing.dict().items()}
                )
                if model.top_provider:
                    if model.top_provider.context_length and model.top_provider.max_completion_tokens:
                        max_context_cost = model.top_provider.context_length * model.sats_pricing.prompt
                        max_completion_cost = model.top_provider.max_completion_tokens * model.sats_pricing.completion
                        model.sats_pricing.max_cost = max_context_cost + max_completion_cost
                else:
                    p = model.sats_pricing.prompt * 1_000_000
                    c = model.sats_pricing.completion * 32_000
                    r = model.sats_pricing.request * 100_000
                    i = model.sats_pricing.image * 100
                    w = model.sats_pricing.web_search * 1000
                    ir = model.sats_pricing.internal_reasoning * 100
                    model.sats_pricing.max_cost = p + c + r + i + w + ir
        except Exception as e:
            logger.error("Error updating sats pricing: %s", e)
        await asyncio.sleep(10)
