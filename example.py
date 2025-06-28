import os

import openai

client = openai.OpenAI(
    api_key=os.environ["CASHU_TOKEN"],
    base_url=os.environ.get("ROUTSTR_API_URL", "https://api.routstr.com/v1"),
    # base_url="http://roustrjfsdgfiueghsklchg.onion/v1",
    # client=httpx.AsyncClient(
    #     proxies={"http": "socks5://localhost:9050"},
    # ),  # to use onion proxy (tor)
)
history: list = []


def chat() -> None:
    while True:
        user_msg = {"role": "user", "content": input("\nYou: ")}
        history.append(user_msg)
        ai_msg = {"role": "assistant", "content": ""}

        for chunk in client.chat.completions.create(
            model=os.environ.get("MODEL", "openai/gpt-4o-mini"),
            messages=history,
            stream=True,
        ):
            if len(chunk.choices) > 0:
                content = chunk.choices[0].delta.content
                if content is not None:
                    ai_msg["content"] += content
                    print(content, end="", flush=True)
        print()
        history.append(ai_msg)


if __name__ == "__main__":
    chat()
