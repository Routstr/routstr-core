import os

from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("TOKEN"),
    base_url=os.environ["API_URL"],
)

response = client.responses.create(
    model="gpt-5-mini",
    input="What is the latest news about AI?",
    tools=[{"type": "web_search"}],  # type: ignore
)

print(response.output)
