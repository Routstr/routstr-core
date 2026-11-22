"""Process-level billing of OpenRouter long-context pricing tiers (PR #879).

Runs a real Routstr process (uvicorn, SQLite file) against a real Cashu mint and
the fake upstream, prices a model with OpenRouter's ``pricing.overrides`` shape
(5x every rate above 100k prompt tokens) and pays each completion with an
``X-Cashu`` token. The charge (``X-Routstr-Cost-Msats``) and the change token
must match the tier the whole prompt, cache tokens included, lands in; a fast
mode response multiplies the tier rate.

Only the BTC/USD quote is pinned, so the sats rates are deterministic and no
exchange is contacted. Needs a Nutshell mint with the FakeWallet backend, as
described in ``test_x_cashu_restart.py``::

    E2E_MINT_URL=http://127.0.0.1:3338 pytest tests/e2e/test_long_context_pricing.py
"""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from routstr.cashu_compat import install_cashu_httpx_shim

MINT_URL = os.environ.get("E2E_MINT_URL", "http://127.0.0.1:3338")
ROOT = Path(__file__).resolve().parents[2]
MODEL = "e2e-model"
ADMIN_PASSWORD = "e2e-admin-password"
BTC_USD = 100_000.0  # one sat is 0.001 USD
THRESHOLD = 100_000
PAYMENT_SATS = 1_000

USD_PRICING: dict[str, Any] = {
    "prompt": "0.0000003",
    "completion": "0.0000015",
    "request": "0",
    "input_cache_read": "0.00000003",
    "overrides": [
        {
            "min_prompt_tokens": THRESHOLD,
            "prompt": "0.0000015",
            "completion": "0.0000075",
            "input_cache_read": "0.00000015",
        }
    ],
}

pytestmark = [pytest.mark.requires_real_mint, pytest.mark.slow]


def _mint_reachable() -> bool:
    try:
        return httpx.get(f"{MINT_URL}/v1/info", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


if not _mint_reachable():
    pytest.skip(f"no Cashu mint at {MINT_URL}", allow_module_level=True)

install_cashu_httpx_shim()

# Pin the quote ``_update_prices`` reads, then serve the production app.
BOOTSTRAP = f"""
import uvicorn
import routstr.payment.price as price

async def pinned_btc_usd() -> float:
    return {BTC_USD!r}

price._fetch_btc_usd_price = pinned_btc_usd
uvicorn.run("routstr.core.main:app", host="127.0.0.1", port=int(__import__("sys").argv[1]))
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wallet(directory: str) -> Any:
    from cashu.wallet.wallet import Wallet

    wallet = await Wallet.with_db(
        MINT_URL, db=directory, load_all_keysets=True, unit="sat"
    )
    await wallet.load_mint()
    return wallet


async def mint_token(amount: int) -> tuple[str, int]:
    """A fresh token and the mint's input fee for redeeming it."""
    with tempfile.TemporaryDirectory() as directory:
        wallet = await _wallet(directory)
        quote = await wallet.request_mint(amount)
        proofs = await wallet.mint(amount, quote_id=quote.quote)
        return str(await wallet.serialize_proofs(proofs)), wallet.get_fees_for_proofs(
            proofs
        )


def proofs_amount(token: str) -> int:
    from cashu.wallet.helpers import deserialize_token_from_string

    return sum(p.amount for p in deserialize_token_from_string(token).proofs)


class Stack:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.upstream_port = _free_port()
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.upstream = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests/e2e/fake_upstream.py"),
                str(self.upstream_port),
            ],
            env={"PATH": os.environ["PATH"]},
        )
        self.routstr: subprocess.Popen[bytes] | None = None
        self.provider_fee = 1.0

    def start(self) -> None:
        # A clean environment: provider API keys in the caller's shell would
        # otherwise be seeded as live upstreams.
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(ROOT),
            "HOME": str(self.directory),
            "DATABASE_URL": f"sqlite+aiosqlite:///{self.directory}/routstr.db",
            "UPSTREAM_BASE_URL": f"http://127.0.0.1:{self.upstream_port}/v1",
            "UPSTREAM_API_KEY": "e2e-upstream-key",
            "CASHU_MINTS": MINT_URL,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
            "ROUTSTR_SECRET_KEY": "l_Tkp-7xmjcQ-IFhr6qhILrU8HPRbEmYMrfSbo_5srU=",
        }
        migrations = self.directory / "migrations"
        if not migrations.exists():
            migrations.symlink_to(ROOT / "migrations")
        log = open(self.directory / "routstr.log", "wb")
        self.routstr = subprocess.Popen(
            [sys.executable, "-c", BOOTSTRAP, str(self.port)],
            cwd=self.directory,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/v1/info", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            assert self.routstr.poll() is None, "routstr exited during startup"
            time.sleep(0.5)
        raise AssertionError("routstr did not start")

    def add_model(self) -> None:
        """Price the model with long-context tiers."""
        token = httpx.post(
            f"{self.url}/admin/api/login", json={"password": ADMIN_PASSWORD}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        providers = httpx.get(
            f"{self.url}/admin/api/upstream-providers", headers=headers
        ).json()
        [provider] = [p for p in providers if str(self.upstream_port) in p["base_url"]]
        response = httpx.post(
            f"{self.url}/admin/api/upstream-providers/{provider['id']}/models",
            headers=headers,
            json={
                "id": MODEL,
                "name": MODEL,
                "description": "e2e",
                "created": 1,
                "context_length": 200_000,
                "architecture": {
                    "modality": "text->text",
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "tokenizer": "Other",
                },
                "pricing": USD_PRICING,
                "top_provider": {
                    "context_length": 200_000,
                    "max_completion_tokens": 8_192,
                },
            },
            timeout=30,
        )
        assert response.status_code < 300, response.text
        self.provider_fee = float(provider["provider_fee"])

    def sats_pricing(self) -> dict[str, Any]:
        """The listed sats rates, once the tiers reach the catalog."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            listed = httpx.get(f"{self.url}/v1/models", timeout=10).json()["data"]
            for model in listed:
                pricing = model.get("sats_pricing") or {}
                if model["id"] == MODEL and pricing.get("overrides"):
                    return dict(pricing)
            time.sleep(0.5)
        raise AssertionError("tiered model never reached the catalog")

    def close(self) -> None:
        for process in (self.routstr, self.upstream):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    stack = Stack(tmp_path_factory.mktemp("long-context"))
    try:
        stack.start()
        stack.add_model()
        yield stack
    finally:
        stack.close()


def _expected_msats(
    rates: dict[str, float], usage: dict[str, Any], factor: float
) -> int:
    """``_calculate_from_tokens`` over OpenAI-shaped usage, from listed sats rates."""
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
    per_1k = {key: rate * 1_000_000.0 * factor for key, rate in rates.items()}
    return math.ceil(
        round((usage["prompt_tokens"] - cached) / 1000 * per_1k["prompt"], 3)
        + round(usage["completion_tokens"] / 1000 * per_1k["completion"], 3)
        + round(cached / 1000 * per_1k["input_cache_read"], 3)
    )


def _usage(prompt: int, completion: int, cached: int = 0, **extra: str) -> dict:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        **extra,
    }
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return usage


# (case, usage, tiered, usage multiplier). ``prompt_tokens`` includes cache.
CASES = [
    ("below threshold", _usage(90_000, 1_000), False, 1.0),
    ("at threshold is exclusive", _usage(THRESHOLD, 1_000), False, 1.0),
    ("above threshold", _usage(120_000, 1_000), True, 1.0),
    ("cache pushes prompt over", _usage(110_000, 1_000, cached=60_000), True, 1.0),
    ("fast mode on the tier", _usage(120_000, 1_000, speed="fast"), True, 2.0),
]


def test_listed_sats_tiers_follow_usd_tiers(stack: Stack) -> None:
    fee = stack.provider_fee
    listed = stack.sats_pricing()
    sats_per_usd = 1 / (BTC_USD / 100_000_000)
    [tier] = listed["overrides"]
    [usd_tier] = USD_PRICING["overrides"]
    assert tier["min_prompt_tokens"] == THRESHOLD
    for field in ("prompt", "completion", "input_cache_read"):
        assert listed[field] == pytest.approx(
            float(USD_PRICING[field]) * fee * sats_per_usd
        )
        assert tier[field] == pytest.approx(float(usd_tier[field]) * fee * sats_per_usd)


@pytest.mark.parametrize(
    ("usage", "tiered", "factor"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
async def test_x_cashu_charge_matches_tier(
    stack: Stack, usage: dict, tiered: bool, factor: float
) -> None:
    listed = stack.sats_pricing()
    rates = {
        field: float((listed["overrides"][0] if tiered else listed)[field])
        for field in ("prompt", "completion", "input_cache_read")
    }
    expected = _expected_msats(rates, usage, factor)

    token, fees = await mint_token(PAYMENT_SATS)
    body: dict[str, Any] = {
        "model": MODEL,
        "user": "usage=" + json.dumps(usage),
        "max_tokens": usage["completion_tokens"],
        "messages": [{"role": "user", "content": "hi"}],
    }
    if factor != 1.0:
        body["speed"] = "fast"
    response = httpx.post(
        f"{stack.url}/v1/chat/completions",
        headers={"X-Cashu": token},
        json=body,
        timeout=60,
    )
    assert response.status_code == 200, response.text

    charged = int(response.headers["X-Routstr-Cost-Msats"])
    change = proofs_amount(response.headers["X-Cashu"])
    print(
        f"usage={usage} tiered={tiered} factor={factor} "
        f"expected_msats={expected} charged_msats={charged} "
        f"paid_sats={PAYMENT_SATS} mint_fee_sats={fees} change_sats={change}"
    )
    assert charged == expected
    assert change == PAYMENT_SATS - fees - math.ceil(charged / 1000)
