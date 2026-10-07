#!/usr/bin/env python3
"""First boot and read-only readiness checks for a provider node.

Public mode (the default, selected with ``--public-url``) assumes DNS, TLS and a
reverse proxy already forward an HTTPS origin to the loopback port. The node
generates its secrets on first boot, publishes its Nostr listing immediately and
is reachable from the network. ``--private`` keeps the node on loopback and
publishes nothing until a public URL is configured later.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8000
COMPOSE = ("docker", "compose", "-f", "compose.node.yml")


def _env_value(text: str, key: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.*)$", text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def _apply_overrides(text: str, overrides: dict[str, str]) -> str:
    remaining = dict(overrides)
    lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines.append(f"{key}={remaining.pop(key)}")
        else:
            lines.append(line)
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def _create_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)


def prepare_env(root: Path, overrides: dict[str, str] | None = None) -> bool:
    """Create or update ``.env``; return True when it was created.

    Only the keys in ``overrides`` are written. An existing ``.env`` is never
    blindly overwritten: identity/onion settings are refused outright and a
    conflicting ``HTTP_URL`` is an error, so an operator edit is never silently
    clobbered.
    """
    overrides = dict(overrides or {})
    env = root / ".env"
    if env.is_symlink():
        raise RuntimeError("Refusing symlinked .env")
    if env.exists():
        if not env.is_file():
            raise RuntimeError(".env must be a regular file")
        text = env.read_text()
        if re.search(r"^\s*(?:NSEC|ONION_URL)\s*=\s*[^\s#]+", text, re.MULTILINE):
            raise RuntimeError(
                "Existing .env sets identity/onion values; manage those from the deployment guide"
            )
        existing_url = _env_value(text, "HTTP_URL")
        requested_url = overrides.get("HTTP_URL")
        if existing_url and requested_url and existing_url != requested_url:
            raise RuntimeError("Existing .env sets a different HTTP_URL; update it explicitly")
        updated = _apply_overrides(text, overrides)
        if updated != text:
            _write_private(env, updated)
        return False
    # O_EXCL avoids overwriting an existing deployment or a concurrent first run.
    with (root / ".env.example").open() as source:
        content = _apply_overrides(source.read(), overrides)
    try:
        _create_private(env, content)
    except BaseException:
        env.unlink(missing_ok=True)
        raise
    return True


def get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("Node returned a non-200 status")
        data = json.load(response)
    if not isinstance(data, dict):
        raise RuntimeError("Expected a JSON object")
    return data


def _fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            return None
        return json.load(response)


def probe(url: str) -> tuple[str, int]:
    info = get_json(url + "/v1/info")
    models = get_json(url + "/v1/models")
    if not isinstance(info.get("name"), str):
        raise RuntimeError("Invalid node info")
    if not isinstance(models.get("data"), list):
        raise RuntimeError("Invalid model list")
    return info["name"], len(models["data"])


def public_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Public URL must be an HTTPS origin without credentials, path or query")
    return value.rstrip("/")


def validate_payout_address(value: str) -> tuple[str, bool]:
    """Return ``(address, verified)``; raise ``ValueError`` when malformed.

    ``verified`` is False when the address is well-formed but could not be
    resolved live (network failure or a bech32 ``lnurl1`` value), so callers can
    warn without blocking onboarding.
    """
    address = value.strip()
    candidate = (
        address[len("lightning:") :]
        if address.lower().startswith("lightning:")
        else address
    )
    if "@" in candidate:
        user, _, host = candidate.partition("@")
        if not user or not host or "@" in host or "/" in host:
            raise ValueError("Lightning address must be user@host")
        probe_url = f"https://{host}/.well-known/lnurlp/{urllib.parse.quote(user)}"
    elif candidate.startswith("https://"):
        probe_url = candidate
    elif candidate.lower().startswith("lnurl1"):
        return address, False
    else:
        raise ValueError(
            "Payout address must be user@host, lnurl1…, or an https:// LNURL-pay URL"
        )
    try:
        data = _fetch_json(probe_url)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return address, False
    if isinstance(data, dict) and data.get("tag") == "payRequest":
        return address, True
    raise ValueError("Payout address does not resolve to a Lightning payRequest")


def preflight_origin(origin: str) -> None:
    """Require an HTTPS answer from the public origin before we publish to it.

    Any HTTP status counts (a proxy returning 502 while the node boots is fine);
    a DNS/TLS/connection failure means the origin is not ready and would
    advertise a dead endpoint.
    """
    request = urllib.request.Request(
        origin + "/v1/info", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read(1)
    except urllib.error.HTTPError:
        return
    except OSError as exc:
        raise RuntimeError(
            f"Public origin {origin} is not reachable over HTTPS; "
            "configure DNS, TLS and the reverse proxy first, or use --private"
        ) from exc


def _wait_ready(local_url: str, timeout: float = 180.0) -> tuple[str, int]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return probe(local_url)
        except (OSError, ValueError, RuntimeError, KeyError):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Node did not become ready in 180s; inspect container status privately"
                ) from None
            time.sleep(3)


def _start(args: argparse.Namespace, local_url: str) -> int:
    if not shutil.which("docker"):
        raise RuntimeError("Docker Compose is required")

    public_origin = public_url(args.public_url) if args.public_url else None
    overrides: dict[str, str] = {}
    if public_origin:
        preflight_origin(public_origin)
        overrides["HTTP_URL"] = public_origin

    payout_verified = True
    if args.ln_address:
        address, payout_verified = validate_payout_address(args.ln_address)
        overrides["RECEIVE_LN_ADDRESS"] = address
    if args.min_payout_sat is not None:
        overrides["MIN_PAYOUT_SAT"] = str(args.min_payout_sat)
    if args.payout_interval is not None:
        overrides["PAYOUT_INTERVAL_SECONDS"] = str(args.payout_interval)

    if prepare_env(ROOT, overrides):
        print("Created .env with owner-only permissions. Review it before public deployment.")

    compose_env = {**os.environ, "ROUTSTR_NODE_PORT": str(args.port)}
    subprocess.run(
        [*COMPOSE, "config", "--quiet"],
        cwd=ROOT,
        env=compose_env,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        [*COMPOSE, "up", "-d", "--build"],
        cwd=ROOT,
        env=compose_env,
        check=True,
        stdout=subprocess.DEVNULL,
    )

    name, count = _wait_ready(local_url)
    mode = "public" if public_origin else "private (nothing published)"
    print(f"Node ready ({mode}): {name}; {count} models.")

    if public_origin:
        try:
            pub_name, pub_count = probe(public_origin)
            print(f"Public endpoint OK: {public_origin} → {pub_name}; {pub_count} models.")
            if pub_count == 0:
                print(
                    "Listed but not yet selling: add an upstream and enable a model "
                    "in the dashboard."
                )
        except (OSError, ValueError, RuntimeError, KeyError):
            print(
                f"Warning: {public_origin} did not serve /v1/info yet; the listing is "
                "corrected automatically once the proxy reaches the node.",
                file=sys.stderr,
            )
    else:
        print(
            "Private mode: the node is loopback-only and has published nothing. "
            "Set a public HTTP URL later to go live."
        )

    if args.ln_address and not payout_verified:
        print(
            "Note: payout address saved but could not be resolved live; it is not a "
            "tested payout.",
            file=sys.stderr,
        )

    print(
        "Phase 2 — back up before configuring: routstr_secret.key, keys.db and "
        ".wallet/. The key decrypts the nsec stored in keys.db."
    )
    print(
        "   Reveal the nsec: docker compose -f compose.node.yml exec routstr "
        "/.venv/bin/python scripts/reveal_nsec.py"
    )
    admin_url = public_origin or local_url
    print(
        f"Phase 3 — dashboard: {admin_url}/admin/ — rotate the admin password, "
        "add an upstream, review payout/pricing."
    )
    check_hint = (
        f"python3 scripts/node_setup.py check --public-url {public_origin}"
        if public_origin
        else "python3 scripts/node_setup.py check"
    )
    print(f"Phase 4 — verify: {check_hint}")
    return 0


def _check(args: argparse.Namespace, local_url: str) -> int:
    url = public_url(args.public_url) if args.public_url else local_url
    name, count = probe(url)
    print(f"Node reachable at {url}: {name}; {count} public models.")
    if count == 0:
        print("Not configured: add an upstream and enable at least one routable model.")
        return 2
    print("Discovery passes; this does not prove paid inference, payouts, or backup recovery.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=("start", "check"))
    parser.add_argument(
        "--public-url",
        help="HTTPS origin: publish on start, or verify from outside with check",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="start loopback-only and publish nothing until a public URL is set later",
    )
    parser.add_argument(
        "--ln-address",
        help="payout Lightning address (user@host, lnurl1…, or https:// LNURL-pay)",
    )
    parser.add_argument(
        "--min-payout-sat", type=int, help="minimum payout balance in sats (default 210)"
    )
    parser.add_argument(
        "--payout-interval", type=int, help="seconds between payout checks (default 900)"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="loopback host port (default: 8000)"
    )
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.min_payout_sat is not None and args.min_payout_sat <= 0:
        parser.error("--min-payout-sat must be positive")
    if args.payout_interval is not None and args.payout_interval <= 0:
        parser.error("--payout-interval must be positive")
    if args.action == "check" and (
        args.private or args.ln_address or args.min_payout_sat or args.payout_interval
    ):
        parser.error("check accepts only --public-url and --port")
    if args.action == "start" and args.private and args.public_url:
        parser.error("--private and --public-url are mutually exclusive")
    if args.action == "start" and not args.private and not args.public_url:
        parser.error("choose --public-url https://your.node (default) or --private")

    local_url = f"http://127.0.0.1:{args.port}"
    if args.action == "start":
        return _start(args, local_url)
    return _check(args, local_url)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        # HTTP response bodies and Docker logs may contain credentials.
        print(
            f"Setup check failed: {type(exc).__name__}. Inspect locally without sharing secrets.",
            file=sys.stderr,
        )
        sys.exit(1)
