import os
import httpx

# Use your Cashu token or API key as the Bearer token
headers = {"Authorization": f"Bearer {os.environ.get('TOKEN')}"}
base_url = os.environ.get("API_URL", "https://api.routstr.com/v1")

resp = httpx.post(f"{base_url}/balance/refund", headers=headers)

print("Refund successful!")
print(resp.json())
