#!/usr/bin/env python3

import asyncio
import json
import os
from typing import TypedDict

import httpx

from routstr.upstream.openrouter_catalog import fetch_openrouter_catalog


class ModelArchitecture(TypedDict):
    modality: str
    input_modalities: list[str]
    output_modalities: list[str]
    tokenizer: str
    instruct_type: str | None


class ModelPricing(TypedDict):
    prompt: str
    completion: str
    request: str
    image: str
    web_search: str
    internal_reasoning: str


class ModelProvider(TypedDict):
    context_length: int
    max_completion_tokens: int | None
    is_moderated: bool


class Model(TypedDict):
    id: str
    name: str
    created: int
    description: str
    context_length: int
    architecture: ModelArchitecture
    pricing: ModelPricing
    top_provider: ModelProvider
    per_request_limits: dict | None


OUTPUT_FILE = os.getenv("OUTPUT_FILE", "models.json")
SOURCE = os.getenv("SOURCE")


def fetch_openrouter_models(source_filter: str | None = None) -> list[dict]:
    """Fetches model information from OpenRouter API."""

    async def fetch() -> list[dict]:
        async with httpx.AsyncClient() as client:
            return await fetch_openrouter_catalog(client)

    models_data = []
    for model in asyncio.run(fetch()):
        model_id = model.get("id", "")
        if source_filter:
            prefix = f"{source_filter}/"
            if not model_id.startswith(prefix):
                continue
            model = {**model, "id": model_id[len(prefix) :]}
            model_id = model["id"]
        if (
            "(free)" in model.get("name", "")
            or model_id == "openrouter/auto"
            or model_id == "google/gemini-2.5-pro-exp-03-25"
        ):
            continue
        models_data.append(model)
    return models_data


def main() -> None:
    source_filter = SOURCE if SOURCE and SOURCE.strip() else None
    models = fetch_openrouter_models(source_filter=source_filter)

    print(f"Writing {len(models)} models to {OUTPUT_FILE}")

    with open(OUTPUT_FILE, "w") as f:
        json.dump({"models": models}, f, indent=4)


if __name__ == "__main__":
    main()
