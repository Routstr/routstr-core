import os

from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("TOKEN"),
    base_url=os.environ.get("API_URL", "https://api.routstr.com/v1"),
)

conversation = []

# First turn
response1 = client.responses.create(  # type: ignore
    model="o4-mini",
    input="Hi, my name is Alice.",
    conversation=conversation,
)
print("Response 1:", response1.output)

# Note: The 'conversation' parameter might need to be constructed differently
# depending on exact SDK/API spec. Typically, you pass back the previous turn's data.
# Assuming the SDK manages or returns a conversation object/ID:
# conversation.append(response1)

# Second turn - demonstrating intent, actual implementation depends on strict API spec
# response2 = client.responses.create(
#     model="openai/gpt-4o-mini",
#     input="What is my name?",
#     conversation=conversation,
# )
# print("Response 2:", response2.output)
