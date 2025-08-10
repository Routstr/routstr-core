#!/usr/bin/env python3

import json
import os
from typing import TypedDict
from urllib.request import urlopen


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
BASE_URL = os.getenv("BASE_URL", "https://openrouter.ai/api/v1")
SOURCE = os.getenv("SOURCE")


def fetch_openrouter_models(source_filter: str | None = None) -> list[Model]:
    """Fetches model information from OpenRouter API."""
    with urlopen(f"{BASE_URL}/models") as response:
        data = json.loads(response.read().decode("utf-8"))

        models_data: list[Model] = []
        for model in data.get("data", []):
            model_id = model.get("id", "")

            if source_filter:
                source_prefix = f"{source_filter}/"
                if not model_id.startswith(source_prefix):
                    continue

                model = dict(model)
                model["id"] = model_id[len(source_prefix) :]
                model_id = model["id"]

            # Skip models with '(free)' in the name or id = 'openrouter/auto'
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
