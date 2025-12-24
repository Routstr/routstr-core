import os

from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("TOKEN"),
    base_url=os.environ.get("API_URL", "https://api.routstr.com/v1"),
)

stream = client.responses.create(
    model="claude-4.5-sonnet",
    input="Write a short poem about rust.",
    stream=True,
)

for event in stream:
    # Note: Depending on the SDK version and response structure,
    # you might access event.output_delta or similar fields
    print(event, end="", flush=True)
print()
