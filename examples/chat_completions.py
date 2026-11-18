import os

from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("TOKEN"),
    base_url=os.environ["API_URL"],
)

response = client.chat.completions.create(
    model=os.environ.get("MODEL", "gpt-5-nano"),
    messages=[{"role": "user", "content": "Hello!"}],
)

print(response.choices[0].message.content)
