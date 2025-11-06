import asyncio
import os

import httpx


async def fetch_models(base_url: str, api_key: str | None = None) -> dict:
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


def parse_model_ids(response: dict) -> list[str]:
    return [model.get("id") for model in response.get("data", []) if "id" in model]


if __name__ == "__main__":
    api_key = os.environ["API_KEY"]
    base_url = os.environ.get("API_BASE_URL", "https://api.openai.com/v1")

    async def main() -> None:
        or_models_response, provider_models_response = await asyncio.gather(
            fetch_models("https://openrouter.ai/api/v1"),
            fetch_models(base_url, api_key),
        )
        provider_model_ids = parse_model_ids(provider_models_response)
        or_models = or_models_response.get("data", [])

        found_models = []
        not_found_models = []

        for model_id in provider_model_ids:
            model = next(
                (
                    model
                    for model in or_models
                    if (model.get("id") == model_id)
                    or (model.get("id").split("/")[-1] == model_id)
                    or (model.get("canonical_slug") == model_id)
                    or (model.get("canonical_slug").split("/")[-1] == model_id)
                ),
                None,
            )
            if model:
                found_models.append(model)
            else:
                not_found_models.append(model_id)
        print("\nFound models:")
        for model in found_models:
            print(model.get("id").split("/")[-1], model.get("pricing").get("prompt"))
        print("\nNot found models:")
        for model in not_found_models:
            print(model)

    asyncio.run(main())
