"""Redeem a cashu token into a balance and pay it out to a Lightning address.

Usage:
    python scripts/refund_token_to_lightning.py <cashu-token> <lightning-address> [--url http://localhost:8000]

Steps:
    1. POST /v1/balance/create   redeems the token into a fresh API key
    2. POST /v1/balance/refund   pays the full balance to the Lightning address

A 502 from the refund means the melt was dispatched but unconfirmed; the
balance is withheld until the server reconciles it. Re-run with the printed
API key to check whether it settled.
"""

import argparse
import ipaddress
import sys
from urllib.parse import urlparse

import httpx


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_url(url: str) -> str:
    """Reject a URL that would put the token and the API key on the wire."""
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and _is_loopback(parsed.hostname or ""):
        return url
    raise SystemExit(
        f"Refusing to send a cashu token and bearer key to {url!r}: "
        "use https, or http only for a loopback host."
    )


def create_balance(client: httpx.Client, token: str) -> str:
    response = client.post("/v1/balance/create", json={"initial_balance_token": token})
    response.raise_for_status()
    data = response.json()
    print(f"Redeemed token: balance {data['balance']} msats, key {data['api_key']}")
    return str(data["api_key"])


def refund_to_lightning(client: httpx.Client, api_key: str, address: str) -> dict:
    response = client.post(
        "/v1/balance/refund",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"lightning_address": address},
    )
    if response.status_code >= 400:
        print(f"Refund failed ({response.status_code}): {response.text}")
        sys.exit(1)
    return dict(response.json())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("token", help="cashu token, or sk-... key from a prior run")
    parser.add_argument("lightning_address", help="Lightning address or LNURL")
    parser.add_argument("--url", default="http://localhost:8000", help="routstr URL")
    args = parser.parse_args()

    with httpx.Client(base_url=check_url(args.url), timeout=120.0) as client:
        api_key = (
            args.token
            if args.token.startswith("sk-")
            else create_balance(client, args.token)
        )
        result = refund_to_lightning(client, api_key, args.lightning_address)

    amount = result.get("sats") or result.get("msats")
    unit = "sats" if "sats" in result else "msats"
    print(
        f"Refund {result['refund_id']} {result['status']}: "
        f"{amount} {unit} -> {result.get('recipient', args.lightning_address)}"
    )


if __name__ == "__main__":
    main()
