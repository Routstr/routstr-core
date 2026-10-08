#!/usr/bin/env python3
"""Opt-in Images API smoke client. Default invocation never sends a request."""

import argparse
import base64
import binascii
import json
import math
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Explicitly permit one generation request",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Explicit dotenv file; never printed or auto-discovered",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Use loopback fake upstream directly, without authentication",
    )
    parser.add_argument("--url", help="Routstr base URL; otherwise ROUTSTR_URL")
    parser.add_argument("--model", default="recraft/recraft-v4.1-flash")
    parser.add_argument(
        "--max-quoted-usd",
        type=float,
        default=0.01,
        help="Maximum accepted catalogue quote including fee",
    )
    args = parser.parse_args()
    if not math.isfinite(args.max_quoted_usd) or args.max_quoted_usd <= 0:
        parser.error("--max-quoted-usd must be finite and positive")
    if not args.execute:
        print(
            "Dry run: no HTTP requests or credentials loaded. Add --execute to generate one image."
        )
        return 0
    if args.env_file:
        try:
            from dotenv import dotenv_values

            values = dotenv_values(args.env_file)
        except (ImportError, OSError):
            print(
                "Unable to read dotenv file (requires project python-dotenv).",
                file=sys.stderr,
            )
            return 1
        for name in ("ROUTSTR_URL", "ROUTSTR_API_KEY", "ROUTSTR_IMAGE_PROVIDER_FEE"):
            if name not in os.environ and isinstance(values.get(name), str):
                os.environ[name] = values[name]
    url = (args.url or os.environ.get("ROUTSTR_URL", "")).rstrip("/")
    parsed = urlsplit(url)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not loopback)
    ):
        parser.error(
            "Use HTTPS or a loopback HTTP URL without credentials, query, or fragment"
        )
    if args.fake and not loopback:
        parser.error("--fake is restricted to loopback")
    key = os.environ.get("ROUTSTR_API_KEY", "")
    if not args.fake and not key:
        parser.error(
            "ROUTSTR_API_KEY (funded Routstr account, not OpenRouter key) is required"
        )
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if not args.fake:
        headers["Authorization"] = f"Bearer {key}"

    def fetch(path, payload=None):
        request = Request(
            url + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers,
        )
        # urllib's default redirect handler could leak credentials. Refuse redirects.
        from urllib.request import HTTPRedirectHandler, build_opener

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *unused):
                return None

        with build_opener(NoRedirect).open(request, timeout=120) as response:
            raw = response.read(40 * 1024 * 1024 + 1)
            if len(raw) > 40 * 1024 * 1024:
                raise ValueError("Response exceeds smoke byte limit")
            return json.loads(raw)

    try:
        if args.fake:
            endpoint = fetch(f"/images/models/{args.model}/endpoints")["endpoints"][0]
            fee = 1.0
        else:
            models = fetch("/models")["data"]
            model = next(
                m
                for m in models
                if m["id"] == args.model or m["id"] == args.model.split("/", 1)[-1]
            )
            capability = model.get("api_capabilities", {}).get("images", {})
            endpoints = capability.get("endpoints", [])
            # Do not invent a fee from public base-rate metadata. Require a
            # server-published customer quote, or use the node's explicit markup.
            fee_text = os.environ.get("ROUTSTR_IMAGE_PROVIDER_FEE")
            if fee_text is None:
                raise ValueError(
                    "Set ROUTSTR_IMAGE_PROVIDER_FEE to the configured node markup for budget preflight"
                )
            fee = float(fee_text)
            endpoint = next(
                e
                for e in endpoints
                if len(e.get("pricing", [])) == 1
                and e["pricing"][0].get("billable") == "output_image"
                and e["pricing"][0].get("unit") == "image"
                and e["pricing"][0].get("variant") is None
            )
        lines = endpoint["pricing"]
        if (
            len(lines) != 1
            or lines[0].get("billable") != "output_image"
            or lines[0].get("unit") != "image"
            or lines[0].get("variant") is not None
        ):
            raise ValueError("Smoke only supports a single fixed output-image price")
        quote = float(lines[0]["cost_usd"]) * fee
        if (
            not math.isfinite(fee)
            or fee < 1
            or not math.isfinite(quote)
            or not 0 < quote <= args.max_quoted_usd
        ):
            raise ValueError("Catalogue quote exceeds smoke budget or is invalid")
        body = {
            "model": args.model,
            "prompt": "A simple red circle on a plain white background.",
            "n": 1,
            "aspect_ratio": "1:1",
            "stream": False,
        }
        if args.fake:
            body["provider"] = {
                "only": [endpoint["provider_tag"]],
                "allow_fallbacks": False,
            }
        result = fetch("/images", body)
        images = result.get("data", [])
        if len(images) != 1:
            raise ValueError("Expected exactly one completed image")
        image = base64.b64decode(images[0]["b64_json"], validate=True)
        if not image:
            raise ValueError("Image payload is empty")
        print(
            f"PASS: one completed image ({len(image)} bytes); preflight quote USD {quote:.6f}."
        )
        print("No image, secret, response body, or refund token was printed or saved.")
        return 0
    except (
        HTTPError,
        URLError,
        ValueError,
        KeyError,
        StopIteration,
        TypeError,
        binascii.Error,
    ):
        print(
            "Smoke failed; response/credentials withheld. Check node logs and capability/budget configuration.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
