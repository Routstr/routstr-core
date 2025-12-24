import os

import httpx
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("TOKEN", ""),
    base_url=os.environ.get("API_URL", "https://api.routstr.com/v1"),
)

for model in client.models.list():
    print(model.id)

# OR

models = httpx.get(
    f"{client.base_url}/v1/models",
    headers={"Authorization": f"Bearer {client.api_key}"},
).json()
