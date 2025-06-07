import openai
from router.settings import require_env, get_env

client = openai.OpenAI(
    api_key=require_env("CASHU_TOKEN"),
    base_url=get_env("ROUTSTR_API_URL", "https://api.routstr.com/v1"),
    # base_url="http://roustrjfsdgfiueghsklchg.onion/v1",
    # client=httpx.AsyncClient(
    #     proxies={"http": "socks5://localhost:9050"},
    # ),  # to use onion proxy (tor)
)
history: list = []


def chat():
    while True:
        user_msg = {"role": "user", "content": input("\nYou: ")}
        history.append(user_msg)
        ai_msg = {"role": "assistant", "content": ""}

        for chunk in client.chat.completions.create(
            model=get_env("MODEL", "openai/gpt-4o-mini"),
            messages=history,
            stream=True,
        ):
            if len(chunk.choices) > 0:
                ai_msg["content"] += chunk.choices[0].delta.content
                print(chunk.choices[0].delta.content, end="", flush=True)
        print()
        history.append(ai_msg)


if __name__ == "__main__":
    chat()
