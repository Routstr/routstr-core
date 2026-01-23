import json
import sys

import httpx


def create_child_keys(base_url: str, api_key: str, count: int = 3) -> list[str]:
    headers = {"Authorization": f"Bearer {api_key}"}

    print(f"Requesting {count} child keys from {base_url}...")

    child_keys = []

    for i in range(count):
        try:
            response = httpx.post(f"{base_url}/v1/balance/child-key", headers=headers)
            if response.status_code == 200:
                data = response.json()
                child_keys.append(data["api_key"])
                print(
                    f"  [{i + 1}] Created: {data['api_key']} (Cost: {data['cost_msats']} msats)"
                )
            else:
                print(f"  [{i + 1}] Failed: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"  [{i + 1}] Error: {str(e)}")

    return child_keys


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python create_child_keys.py <api_key_or_cashu_token> [base_url]")
        sys.exit(1)

    auth_key = sys.argv[1]
    base_url = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"

    keys = create_child_keys(base_url, auth_key)

    if keys:
        print("\nSuccessfully created child keys:")
        print(json.dumps(keys, indent=2))
    else:
        print("\nNo child keys were created.")
