import os

import httpx

# Send a Cashu token to the /create endpoint to get a persistent API key
token = os.environ.get("TOKEN")
if not token:
    print("Please set TOKEN environment variable with a Cashu token")
    exit(1)

base_url = os.environ.get("API_URL", "https://api.routstr.com/v1")

resp = httpx.get(f"{base_url}/balance/create", params={"initial_balance_token": token})

print(resp.json())
