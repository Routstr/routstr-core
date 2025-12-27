import os

from openai import OpenAI

# The OpenAI SDK handles the 'responses' endpoint if it's updated to the latest version
# and the base_url points to a compatible proxy like Routstr.
client = OpenAI(
    api_key=os.environ.get("TOKEN"),
    base_url=os.environ.get("API_URL", "https://api.routstr.com/v1"),
)

response = client.responses.create(
    model="gpt-5-mini",
    input="Tell me a three sentence bedtime story about a unicorn.",
)

print(response.output)
