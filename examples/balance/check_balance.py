import os

import httpx

# Use your Cashu token or API key as the Bearer token,
# cashu token is hashed on the server and acts as an Temporary API key
headers = {"Authorization": f"Bearer {os.environ.get('TOKEN')}"}
base_url = os.environ["API_URL"]

resp = httpx.get(f"{base_url}/balance/info", headers=headers)
print(resp.json())
