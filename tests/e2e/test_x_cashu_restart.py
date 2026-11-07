"""Process-level X-Cashu restart tests for issue #876.

Runs a real Routstr process (uvicorn, SQLite file) against a real Cashu mint
and a slow fake upstream, kills or stops it while a paid request is in flight,
restarts it on the same database, and claims the refund the way a client does:
``POST /v1/balance/refund`` with the original ``X-Cashu`` token. The refund
token must then redeem at the mint.

A refund returns what the node was credited: the payment less the mint's input
fee for redeeming it. The assertions account for that fee, so they hold on a
fee-free mint and on one with input fees.

Needs a Nutshell mint with the FakeWallet backend, for example::

    docker run -d -p 3338:3338 -e MINT_BACKEND_BOLT11_SAT=FakeWallet \\
        -e MINT_LISTEN_HOST=0.0.0.0 -e MINT_LISTEN_PORT=3338 \\
        -e MINT_PRIVATE_KEY=e2e cashubtc/nutshell:0.20.0 python -m cashu.mint

The assertions are fee-aware, so they pass whether or not the mint charges
input fees (``MINT_INPUT_FEE_PPK``).

    E2E_MINT_URL=http://127.0.0.1:3338 pytest tests/e2e -m requires_real_mint
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
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
LIFETIME = 20
CLEANUP = 5
# Recovery waits for the request lifetime, its cleanup window and a fixed
# margin, then runs on a 60 s interval.
RECOVERY_DEADLINE = LIFETIME + CLEANUP + 60 + 60 + 30

pytestmark = [pytest.mark.requires_real_mint, pytest.mark.slow]


def _mint_reachable() -> bool:
    try:
        return httpx.get(f"{MINT_URL}/v1/info", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


if not _mint_reachable():
    pytest.skip(f"no Cashu mint at {MINT_URL}", allow_module_level=True)

install_cashu_httpx_shim()


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


async def mint_token(amount: int) -> str:
    with tempfile.TemporaryDirectory() as directory:
        wallet = await _wallet(directory)
        quote = await wallet.request_mint(amount)
        proofs = await wallet.mint(amount, quote_id=quote.quote)
        return str(await wallet.serialize_proofs(proofs))


async def _proofs(wallet: Any, token: str) -> Any:
    from cashu.wallet.helpers import deserialize_token_from_string

    proofs = deserialize_token_from_string(token).proofs
    # v4 tokens carry short keyset ids.
    await wallet._expand_short_keyset_ids(proofs)
    return proofs


class Stack:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.upstream_port = _free_port()
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.upstream_log = directory / "upstream.jsonl"
        self.upstream_log.touch()
        self.upstream = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests/e2e/fake_upstream.py"),
                str(self.upstream_port),
            ],
            env={
                "PATH": os.environ["PATH"],
                "FAKE_UPSTREAM_DELAY": "300",
                "FAKE_UPSTREAM_LOG": str(self.upstream_log),
            },
        )
        self.routstr: subprocess.Popen[bytes] | None = None
        self.starts = 0

    def start(self, graceful_timeout: int | None = None) -> None:
        self.starts += 1
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
            "FIXED_PRICING": "true",
            "FIXED_COST_PER_REQUEST": "10",
            "FIXED_PER_1K_INPUT_TOKENS": "500",
            "FIXED_PER_1K_OUTPUT_TOKENS": "500",
            "MAX_REQUEST_LIFETIME_SECONDS": str(LIFETIME),
            "REQUEST_CLEANUP_TIMEOUT_SECONDS": str(CLEANUP),
        }
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "routstr.core.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]
        if graceful_timeout is not None:
            command += ["--timeout-graceful-shutdown", str(graceful_timeout)]
        log = open(self.directory / f"routstr-{self.starts}.log", "wb")
        # The node wallet lives in the working directory; a shared one would
        # carry keysets of an earlier mint into this run. Alembic resolves its
        # script location against the working directory too.
        migrations = self.directory / "migrations"
        if not migrations.exists():
            migrations.symlink_to(ROOT / "migrations")
        self.routstr = subprocess.Popen(
            command,
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

    def stop(self, sig: signal.Signals) -> int:
        assert self.routstr is not None
        self.routstr.send_signal(sig)
        return self.routstr.wait(timeout=60)

    def add_model(self) -> None:
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
                "context_length": 8192,
                "architecture": {
                    "modality": "text->text",
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "tokenizer": "Other",
                },
                "pricing": {"prompt": "0", "completion": "0", "request": "0"},
            },
            timeout=30,
        )
        assert response.status_code < 300, response.text

    def upstream_requests(self) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in self.upstream_log.read_text().splitlines()
            if line.strip()
        ]

    def close(self) -> None:
        for process in (self.routstr, self.upstream):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    stack = Stack(tmp_path)
    try:
        stack.start()
        stack.add_model()
        yield stack
    finally:
        stack.close()


def claim(stack: Stack, token: str) -> httpx.Response:
    return httpx.post(
        f"{stack.url}/v1/balance/refund", headers={"X-Cashu": token}, timeout=30
    )


def pay_in_background(stack: Stack, token: str, path: str, body: dict) -> None:
    def send() -> None:
        try:
            httpx.post(
                f"{stack.url}{path}",
                headers={"X-Cashu": token},
                json=body,
                timeout=LIFETIME * 10,
            )
        except httpx.HTTPError:
            pass  # the server goes away mid-request

    threading.Thread(target=send, daemon=True).start()


def wait_for_upstream(stack: Stack, count: int) -> None:
    deadline = time.monotonic() + 30
    while len(stack.upstream_requests()) < count:
        assert time.monotonic() < deadline, "requests never reached the upstream"
        time.sleep(0.2)


SLOW_REQUESTS = [
    (
        "/v1/chat/completions",
        {
            "model": MODEL,
            "user": "slow",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}],
        },
    ),
    (
        "/v1/chat/completions",
        {
            "model": MODEL,
            "user": "slow",
            "max_tokens": 50,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    ),
    (
        "/v1/responses",
        {"model": MODEL, "user": "slow", "input": "hi", "max_output_tokens": 50},
    ),
]


async def _start_slow_requests(stack: Stack, amount: int) -> list[str]:
    tokens = [await mint_token(amount) for _ in SLOW_REQUESTS]
    for token, (path, body) in zip(tokens, SLOW_REQUESTS):
        pay_in_background(stack, token, path, body)
    wait_for_upstream(stack, len(SLOW_REQUESTS))
    for token in tokens:
        assert claim(stack, token).status_code == 425
    return tokens


async def _assert_refunded_in_full(
    token: str, refund: httpx.Response, amount: int
) -> None:
    assert refund.status_code == 200, refund.text
    body = refund.json()
    assert refund.headers["X-Cashu"] == body["token"]
    # One wallet for every check: the mint rate-limits each client.
    with tempfile.TemporaryDirectory() as directory:
        wallet = await _wallet(directory)
        paid = await _proofs(wallet, token)
        refunded = await _proofs(wallet, body["token"])
        assert body["sats"] == str(amount - wallet.get_fees_for_proofs(paid))
        await wallet.redeem(refunded)
        assert wallet.available_balance.amount == (
            int(body["sats"]) - wallet.get_fees_for_proofs(refunded)
        )
        with pytest.raises(Exception, match="(?i)spent"):
            await wallet.redeem(paid)


async def test_crash_mid_request_refunds_in_full_after_restart(stack: Stack) -> None:
    """The reporter's case: the process dies with paid requests in flight."""
    tokens = await _start_slow_requests(stack, 60)

    stack.stop(signal.SIGKILL)
    stack.start()

    answers: dict[str, httpx.Response] = {}
    deadline = time.monotonic() + RECOVERY_DEADLINE
    while len(answers) < len(tokens):
        assert time.monotonic() < deadline, "refunds were never recovered"
        for token in tokens:
            if token in answers:
                continue
            response = claim(stack, token)
            # Still owed: pending until recovery mints the refund, never terminal.
            assert response.status_code in (200, 425), response.text
            if response.status_code == 200:
                answers[token] = response
        time.sleep(2)

    for token in tokens:
        await _assert_refunded_in_full(token, answers[token], 60)


async def test_graceful_stop_refunds_before_exit(stack: Stack) -> None:
    stack.stop(signal.SIGTERM)
    stack.start(graceful_timeout=2)
    tokens = await _start_slow_requests(stack, 60)

    assert stack.stop(signal.SIGTERM) is not None
    stack.start()

    # Refunded during shutdown, so claimable at once without waiting for recovery.
    for token in tokens:
        await _assert_refunded_in_full(token, claim(stack, token), 60)


async def test_change_and_full_charge_are_terminal_across_restarts(
    stack: Stack,
) -> None:
    def ask(token: str, user: str) -> httpx.Response:
        response = httpx.post(
            f"{stack.url}/v1/chat/completions",
            headers={"X-Cashu": token},
            json={
                "model": MODEL,
                "user": user,
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=60,
        )
        assert response.status_code == 200, response.text
        return response

    paid = await mint_token(60)
    change = ask(paid, "fast").headers["X-Cashu"]
    assert 0 < proofs_amount(change) < 60

    # Usage beyond the payment consumes all of it: nothing is owed back.
    exact = await mint_token(60)
    assert "X-Cashu" not in ask(exact, "greedy").headers

    stack.stop(signal.SIGKILL)
    stack.start()
    time.sleep(LIFETIME + CLEANUP + 60 + 5)  # past the recovery bound

    claimed = claim(stack, paid)
    assert claimed.status_code == 200
    assert claimed.json()["token"] == change

    charged = claim(stack, exact)
    assert charged.status_code == 409, charged.text
    error = charged.json()["detail"]["error"]
    assert error["type"] == "token_already_spent"
    assert error["code"] == "payment_fully_charged"


def proofs_amount(token: str) -> int:
    from cashu.wallet.helpers import deserialize_token_from_string

    return sum(p.amount for p in deserialize_token_from_string(token).proofs)
