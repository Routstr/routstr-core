import json
import os
import sys
import urllib.error
import urllib.request
from getpass import getpass


def main() -> None:
    base_url = os.environ.get("ROUTSTR_API_URL", "https://api.routstr.com/v1").rstrip("/")
    url = f"{base_url}/balance/deduct"
    print(f"Using API base URL: {base_url}")

    api_key = getpass("Enter your API key (sk-... or Cashu token): ").strip()
    if not api_key:
        print("API key is required", file=sys.stderr)
        sys.exit(1)

    msat_str = input("Enter amount to deduct (msats): ").strip()
    try:
        msats = int(msat_str)
    except ValueError:
        print("Invalid msat amount; must be an integer", file=sys.stderr)
        sys.exit(1)

    payload = {"msats": msats}
    data = json.dumps(payload).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            try:
                out = json.loads(body.decode("utf-8"))
            except Exception:
                print(body.decode("utf-8", "ignore"))
                return

            new_balance = out.get("balance")
            if new_balance is not None:
                print(f"Deduction successful. New balance (msats): {new_balance}")
            else:
                print(json.dumps(out, indent=2))

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            err = json.loads(body)
            print(f"HTTP {e.code}: {json.dumps(err, indent=2)}", file=sys.stderr)
        except Exception:
            print(f"HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(2)
    except urllib.error.URLError as e:
        print(f"Network error: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
