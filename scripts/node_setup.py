#!/usr/bin/env python3
"""Private first boot and read-only readiness checks for a provider node."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
LOCAL_URL = "http://127.0.0.1:8000"
COMPOSE = ("docker", "compose", "-f", "compose.node.yml")


def prepare_env(root: Path) -> bool:
    env = root / ".env"
    if env.is_symlink():
        raise RuntimeError("Refusing symlinked .env")
    if env.exists():
        if not env.is_file():
            raise RuntimeError(".env must be a regular file")
        if re.search(r"^\s*(?:NSEC|HTTP_URL|ONION_URL)\s*=\s*[^\s#]+", env.read_text(), re.MULTILINE):
            raise RuntimeError("Existing .env has public identity/URL settings; use the deployment guide")
        return False
    # O_EXCL avoids overwriting an existing deployment or a concurrent first run.
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output, (root / ".env.example").open("rb") as source:
            shutil.copyfileobj(source, output)
    except BaseException:
        env.unlink()
        raise
    return True


def get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("Node returned a non-200 status")
        return json.load(response)


def probe(url: str) -> tuple[str, int]:
    info = get_json(url + "/v1/info")
    models = get_json(url + "/v1/models")
    if not isinstance(info, dict) or not isinstance(info.get("name"), str):
        raise RuntimeError("Invalid node info")
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        raise RuntimeError("Invalid model list")
    return info["name"], len(models["data"])


def public_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Public URL must be an HTTPS origin without credentials, path or query")
    return value.rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "check"))
    parser.add_argument("--public-url", help="HTTPS origin to check from this machine (check only)")
    args = parser.parse_args()
    if args.public_url and args.action != "check":
        parser.error("--public-url requires check")
    if args.action == "start":
        if not shutil.which("docker"):
            raise RuntimeError("Docker Compose is required")
        if prepare_env(ROOT):
            print("Created .env with owner-only permissions. Review it before public deployment.")
        subprocess.run([*COMPOSE, "config", "--quiet"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([*COMPOSE, "up", "-d", "--build"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + 180
        while True:
            try:
                name, count = probe(LOCAL_URL)
                break
            except (OSError, ValueError, RuntimeError, KeyError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Node did not become ready in 180s; inspect container status privately") from None
                time.sleep(3)
        print(f"Private node ready: {name}; {count} models. Open {LOCAL_URL}/admin/ locally or use an SSH tunnel.")
        print("The operator must retrieve the first-run password privately from Docker logs and rotate it in Settings → Admin Settings.")
        print("Then add an upstream at Providers, configure payout/pricing, and run 'python3 scripts/node_setup.py check'.")
    else:
        url = public_url(args.public_url) if args.public_url else LOCAL_URL
        name, count = probe(url)
        print(f"Node reachable at {url}: {name}; {count} public models.")
        if count == 0:
            print("Not configured: add an upstream and enable at least one routable model.")
            return 2
        print("Discovery passes; this does not prove paid inference, payouts, or backup recovery.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        # HTTP response bodies and Docker logs may contain credentials.
        print(f"Setup check failed: {type(exc).__name__}. Inspect locally without sharing secrets.", file=sys.stderr)
        sys.exit(1)
