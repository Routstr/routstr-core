import os

from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("TOKEN"),
    base_url=os.environ.get("API_URL", "https://api.routstr.com/v1"),
)

messages = []
while True:
    messages.append({"role": "user", "content": input("\nYou: ")})

    stream = client.chat.completions.create(
        model=os.environ.get("MODEL", "gpt-5.1-mini"),
        messages=messages,  # type: ignore
        stream=True,
    )

    print("AI: ", end="")
    response_content = ""
    for chunk in stream:
        if content := chunk.choices[0].delta.content:  # type: ignore
            print(content, end="", flush=True)
            response_content += content
    print()

    messages.append({"role": "assistant", "content": response_content})
