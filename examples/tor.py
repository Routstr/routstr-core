import os

import httpx
from openai import OpenAI

# Requires `pip install "httpx[socks]"` and a running Tor proxy on port 9050
client = OpenAI(
    api_key=os.environ.get("TOKEN"),
    base_url=os.environ.get("ONION_URL", "http://roustrjfsdgfiueghsklchg.onion/v1"),
    http_client=httpx.Client(proxies="socks5://localhost:9050"),
)

print(
    client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "Hello from Tor!"}],
    )
    .choices[0]
    .message.content
)
