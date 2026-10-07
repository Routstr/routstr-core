"""Live integration test for TLSN verified mode: routstr-core's
``forward_verified_via_proverd`` against a REAL proverd, a REAL TLS mock
upstream, and the native Rust test verifier.

Full loop:
  python (channel A) → proverd → relayed TLS → mock upstream
  native verifier (channels B+C) → ZK proof → disclosed transcript

Assertions: response is byte-equal to the proven upstream body; the disclosed
request carries the exact JSON we sent; the credential value stays redacted;
verified/cost headers are present.

Requires prebuilt binaries (built once by the dev/harness):
  <proverd-repo>/target/debug/proverd
  <proverd-repo>/target/debug/tlsn-verifier
  <proverd-repo>/target/debug/examples/mock_upstream
Resolved from PROVERD_BIN, TLSN_VERIFIER_BIN, MOCK_UPSTREAM_BIN,
TLSN_FIXTURE_CA_PEM and TLSN_LAB_DIR, falling back to the development
layout. Skips loudly when they are missing.
"""

import asyncio
import base64
import json
import socket
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import routstr.upstream.tlsn_verified as tlsn_verified
from routstr.core.settings import settings
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.generic import GenericUpstreamProvider

# Development layout: <provable-ai>/routstr-core/tests/integration/... — the
# lab root is three levels up. Worktrees/harnesses set TLSN_LAB_DIR.
LAB = Path(os.environ.get("TLSN_LAB_DIR", str(Path(__file__).resolve().parents[3])))
PROVERD_BIN = Path(os.environ.get("PROVERD_BIN", LAB / "proverd/target/debug/proverd"))
VERIFIER_BIN = Path(
    os.environ.get("TLSN_VERIFIER_BIN", LAB / "proverd/target/debug/tlsn-verifier")
)
MOCK_BIN = Path(
    os.environ.get(
        "MOCK_UPSTREAM_BIN", LAB / "proverd/target/debug/examples/mock_upstream"
    )
)
ROOT_CA_PEM = Path(
    os.environ.get(
        "TLSN_FIXTURE_CA_PEM",
        LAB / "tlsn/crates/server-fixture/certs/src/tls/root_ca.crt",
    )
)
SERVER_DOMAIN = "test-server.io"
AUTH_TOKEN = "random_auth_token"

BINARIES = [PROVERD_BIN, VERIFIER_BIN, MOCK_BIN, ROOT_CA_PEM]

_MISSING = [str(p) for p in BINARIES if not p.exists()]
if _MISSING:
    print(f"[test_tlsn_verified_live] SKIPPING: missing {', '.join(_MISSING)}")

pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=f"proverd/tlsn-verifier/mock_upstream not built (missing {_MISSING})",
)

MODEL = Model(
    id="gpt-mock",
    name="gpt-mock",
    created=0,
    description="",
    context_length=64_000,
    architecture=Architecture(
        modality="text->text",
        input_modalities=["text"],
        output_modalities=["text"],
        tokenizer="Other",
        instruct_type=None,
    ),
    pricing=Pricing(prompt=0.001, completion=0.002),
    sats_pricing=Pricing(prompt=0.001, completion=0.002),
)

CHAT_REQUEST = {
    "model": "gpt-mock",
    "messages": [{"role": "user", "content": "live integration hello"}],
}

COST_DATA = {
    "total_msats": 19,
    "charged_msats": 19,
    "input_msats": 12,
    "output_msats": 7,
    "total_usd": 0.0,
}


def http_body_from_transcript(recv: bytes) -> bytes:
    """Extract the HTTP message body from a disclosed received transcript:
    strip headers, de-chunk when transfer-encoded (the SDK comparator does
    the same — wire bytes include HTTP/1.1 framing)."""
    head, _, rest = recv.partition(b"\r\n\r\n")
    headers = {
        k.lower(): v.strip()
        for line in head.split(b"\r\n")[1:]
        for k, _, v in [line.partition(b":")]
    }
    if b"chunked" not in headers.get(b"transfer-encoding", b""):
        return rest
    out = bytearray()
    i = 0
    while True:
        j = rest.index(b"\r\n", i)
        size = int(rest[i:j].split(b";")[0], 16)
        i = j + 2
        if size == 0:
            break
        out += rest[i : i + size]
        i += size + 2
    return bytes(out)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http_ready(port: int, timeout: float = 10.0) -> None:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"proverd on :{port} did not become ready")


@pytest.fixture(scope="module")
def mock_upstream() -> Any:
    proc = subprocess.Popen(
        [str(MOCK_BIN), "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdout is not None
    first = proc.stdout.readline().strip()
    assert first.startswith("PORT="), f"mock_upstream did not print PORT=, got {first!r}"
    port = int(first.split("=", 1)[1])
    yield port
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def proverd() -> Any:
    port = free_port()
    proc = subprocess.Popen(
        [str(PROVERD_BIN)],
        env={
            "PATH": "/usr/bin:/bin",
            "PROVERD_BIND": f"127.0.0.1:{port}",
            "PROVERD_EXTRA_ROOT_CERT_PEM": str(ROOT_CA_PEM),
            "RUST_LOG": "proverd=info,tlsn=warn",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_http_ready(port)
    yield port
    proc.terminate()
    proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_live_verified_non_streaming(
    mock_upstream: int,
    proverd: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_id = "live-m2-1"
    transcript_path = tmp_path / "transcript.json"

    verifier_proc = subprocess.Popen(
        [
            str(VERIFIER_BIN),
            "--proverd", f"ws://127.0.0.1:{proverd}",
            "--session", session_id,
            "--upstream", f"127.0.0.1:{mock_upstream}",
            "--root-cert-pem", str(ROOT_CA_PEM),
            "--transcript-out", str(transcript_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    monkeypatch.setattr(settings, "tlsn_proverd_url", f"http://127.0.0.1:{proverd}")
    monkeypatch.setattr(
        tlsn_verified,
        "adjust_payment_for_tokens",
        AsyncMock(return_value=COST_DATA),
    )

    request = MagicMock()
    request.headers = {
        "x-routstr-verify": "tlsn-proxy",
        "x-routstr-tlsn-session": session_id,
    }
    request.method = "POST"
    request.query_params = {}

    provider = GenericUpstreamProvider(
        base_url=f"https://{SERVER_DOMAIN}:{mock_upstream}", api_key=AUTH_TOKEN
    )

    body = json.dumps(CHAT_REQUEST).encode()
    t0 = time.time()
    response = await tlsn_verified.forward_verified_via_proverd(
        provider=provider,
        request=request,
        path="v1/chat/completions",
        headers={
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "content-type": "application/json",
        },
        request_body=body,
        key=MagicMock(hashed_key="deadbeefcafe"),
        max_cost_for_model=1000,
        session=MagicMock(),
        model_obj=MODEL,
        reservation_snapshot=None,
        url=f"https://{SERVER_DOMAIN}:{mock_upstream}/v1/chat/completions",
        original_model_id="gpt-mock",
    )
    elapsed = time.time() - t0

    # --- channel A response: byte-passthrough + headers ---
    assert response.status_code == 200
    assert response.headers["x-routstr-verified"] == "tlsn-proxy"
    assert response.headers["x-routstr-upstream-host"] == SERVER_DOMAIN
    assert response.headers["x-routstr-tlsn-session"] == session_id
    assert response.headers["x-routstr-cost-msats"] == "19"
    response_json = json.loads(response.body)
    assert response_json["model"] == "gpt-mock"
    assert "cost" not in response_json  # no body mutation

    # --- verifier completed and verified the proof ---
    rc = await asyncio.to_thread(verifier_proc.wait, timeout=60)
    assert rc == 0, (
        f"verifier failed: {verifier_proc.stdout.read()}\n{verifier_proc.stderr.read()}"
    )
    transcript = json.loads(transcript_path.read_text())
    assert transcript["server_name"] == SERVER_DOMAIN

    # disclosed request == what we sent, credential redacted
    sent = base64.b64decode(transcript["sent_b64"])
    sent_str = sent.decode("utf-8", errors="replace")
    assert "POST /v1/chat/completions" in sent_str
    assert AUTH_TOKEN not in sent_str, "REDACTION FAILURE: credential revealed"
    assert "authorization:" in sent_str.lower()
    sent_body = sent.split(b"\r\n\r\n", 1)[1]
    assert json.loads(sent_body) == CHAT_REQUEST  # messages deep-equal

    # disclosed response body == channel A body (byte equality)
    recv = base64.b64decode(transcript["recv_b64"])
    proven_body = recv.split(b"\r\n\r\n", 1)[1]
    assert proven_body == response.body

    print(
        f"\nLIVE M2 verified ✓ {SERVER_DOMAIN} in {elapsed:.2f}s "
        f"(verifier commit={transcript['commit_ms']}ms proof={transcript['proof_ms']}ms)"
    )


@pytest.mark.asyncio
async def test_live_verified_streaming(
    mock_upstream: int,
    proverd: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_id = "live-m2-sse-1"
    transcript_path = tmp_path / "transcript-sse.json"

    verifier_proc = subprocess.Popen(
        [
            str(VERIFIER_BIN),
            "--proverd", f"ws://127.0.0.1:{proverd}",
            "--session", session_id,
            "--upstream", f"127.0.0.1:{mock_upstream}",
            "--root-cert-pem", str(ROOT_CA_PEM),
            "--transcript-out", str(transcript_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    monkeypatch.setattr(settings, "tlsn_proverd_url", f"http://127.0.0.1:{proverd}")
    payment = AsyncMock(return_value=COST_DATA)
    monkeypatch.setattr(tlsn_verified, "adjust_payment_for_tokens", payment)
    fake_session = MagicMock()
    fake_session.get = AsyncMock(return_value=MagicMock(hashed_key="deadbeefcafe"))

    class _FakeSessionCM:
        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(tlsn_verified, "create_session", lambda: _FakeSessionCM())

    request = MagicMock()
    request.headers = {
        "x-routstr-verify": "tlsn-proxy",
        "x-routstr-tlsn-session": session_id,
    }
    request.method = "POST"
    request.query_params = {}

    provider = GenericUpstreamProvider(
        base_url=f"https://{SERVER_DOMAIN}:{mock_upstream}", api_key=AUTH_TOKEN
    )

    chat_request = {
        **CHAT_REQUEST,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.time()
    response = await tlsn_verified.forward_verified_via_proverd(
        provider=provider,
        request=request,
        path="v1/chat/completions",
        headers={
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "content-type": "application/json",
        },
        request_body=json.dumps(chat_request).encode(),
        key=MagicMock(hashed_key="deadbeefcafe"),
        max_cost_for_model=1000,
        session=MagicMock(),
        model_obj=MODEL,
        reservation_snapshot=None,
        url=f"https://{SERVER_DOMAIN}:{mock_upstream}/v1/chat/completions",
        original_model_id="gpt-mock",
    )

    assert response.status_code == 200
    assert response.headers["x-routstr-verified"] == "tlsn-proxy"
    assert response.headers["content-type"].startswith("text/event-stream")

    # Stream begins immediately (first chunk before proof exists).
    chunks = [chunk async for chunk in response.body_iterator]
    sse_bytes = b"".join(chunks)
    elapsed = time.time() - t0
    assert b"data: [DONE]" in sse_bytes
    assert b"chat.completion.chunk" in sse_bytes

    # Billing settled from the upstream usage chunk.
    assert payment.await_count == 1
    payload_json = payment.await_args.args[1]
    assert payload_json["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 7,
        "total_tokens": 19,
    }

    # Verifier: proof checks out; disclosed SSE bytes equal what we streamed.
    rc = await asyncio.to_thread(verifier_proc.wait, timeout=60)
    assert rc == 0, (
        f"verifier failed: {verifier_proc.stdout.read()}\n{verifier_proc.stderr.read()}"
    )
    transcript = json.loads(transcript_path.read_text())
    sent = base64.b64decode(transcript["sent_b64"])
    assert AUTH_TOKEN.encode() not in sent.replace(b"\x00", b"")
    recv = base64.b64decode(transcript["recv_b64"])
    proven_body = http_body_from_transcript(recv)
    assert proven_body == sse_bytes

    print(
        f"\nLIVE M2 verified SSE ✓ {SERVER_DOMAIN} in {elapsed:.2f}s "
        f"(verifier commit={transcript['commit_ms']}ms proof={transcript['proof_ms']}ms)"
    )
