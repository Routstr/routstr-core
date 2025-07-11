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


def fetch_openrouter_models() -> list[Model]:
    """Fetches model information from OpenRouter API."""
    with urlopen(f"{BASE_URL}/models") as response:
        data = json.loads(response.read().decode("utf-8"))

        models_data: list[Model] = []
        for model in data.get("data", []):
            # Skip models with '(free)' in the name or id = 'openrouter/auto'
            if (
                "(free)" in model.get("name", "")
                or model.get("id") == "openrouter/auto"
            ):
                continue
            # Skip free Gemini 2.5 Pro Exp
            if model.get("id") == "google/gemini-2.5-pro-exp-03-25":
                continue

            models_data.append(model)

        return models_data


def main() -> None:
    models = fetch_openrouter_models()

    # Print the first model data in a nicely indented JSON format
    # print(json.dumps(models[0], indent=4))
    print(f"Writing {len(models)} models to {OUTPUT_FILE}")

    with open(OUTPUT_FILE, "w") as f:
        json.dump({"models": models}, f, indent=4)


if __name__ == "__main__":
    main()
