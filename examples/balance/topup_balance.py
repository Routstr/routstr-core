import os

import httpx

# Use your Cashu token or API key as the Bearer token
headers = {"Authorization": f"Bearer {os.environ.get('TOKEN')}"}
base_url = os.environ.get("API_URL", "https://api.routstr.com/v1")

# The Cashu token to top up with
cashu_token = input("Enter Cashu token to top up: ")

resp = httpx.post(
    f"{base_url}/balance/topup", headers=headers, json={"cashu_token": cashu_token}
)

print(resp.json())
