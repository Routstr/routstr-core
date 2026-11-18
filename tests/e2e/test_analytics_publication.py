"""Opt-in socket/process E2E. Use an EMPTY disposable PostgreSQL database.

ROUTSTR_E2E_POSTGRES_URL=postgresql+asyncpg://... pytest tests/e2e/test_analytics_publication.py

No real mint, model service or public relay is contacted. Only external price,
provider discovery and maintenance jobs are stubbed; HTTP routing, reservation,
settlement, collection, SQL, signing and relay OK/readback use production code.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request

ROOT = Path(__file__).resolve().parents[2]
KEY = "11" * 32  # Publicly known, test-only signing key.
BALANCE = 1_000_000


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _worker(port: int, upstream_port: int) -> None:
    import uvicorn

    import routstr.core.main as main
    import routstr.payment.cost_calculation as costs
    import routstr.payment.price as prices
    import routstr.proxy as proxy
    from routstr.core import terminal_outcomes
    from routstr.payment.models import Architecture, Model, Pricing
    from routstr.upstream.base import BaseUpstreamProvider

    async def idle(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    async def noop(*args: Any, **kwargs: Any) -> None:
        pass

    class Provider(BaseUpstreamProvider):
        def get_cached_models(self) -> list[Model]:
            return [
                Model(
                    id="e2e/model",
                    name="e2e",
                    created=1,
                    description="fixture",
                    context_length=4096,
                    architecture=Architecture(
                        modality="text",
                        input_modalities=["text"],
                        output_modalities=["text"],
                        tokenizer="gpt",
                        instruct_type=None,
                    ),
                    pricing=Pricing(prompt=0.001, completion=0.002, max_cost=50),
                    sats_pricing=Pricing(prompt=0.001, completion=0.002, max_cost=50),
                )
            ]

        async def refresh_models_cache(self) -> None:
            pass

    async def install() -> None:
        provider = Provider(f"http://127.0.0.1:{upstream_port}/v1", "fixture", 1.0)
        provider.provider_type = "custom"
        proxy._upstreams = [provider]
        await proxy.refresh_model_maps()

    # Accelerate lease recovery, not outcome timestamps or settlement.
    terminal_outcomes.terminal_outcome_writer._heartbeat_seconds = 0.05
    terminal_outcomes.terminal_outcome_writer._lease_timeout_ms = 500
    main.initialize_upstreams = install
    prices._update_prices = noop
    costs.sats_usd_price = lambda: 0.0005
    for name in (
        "update_prices_periodically",
        "update_sats_pricing",
        "periodic_payout",
        "announce_provider",
        "providers_cache_refresher",
        "refresh_model_maps_periodically",
        "sync_upstreams_periodically",
        "periodic_stale_reservation_sweep",
        "periodic_dead_key_prune",
        "periodic_auto_topup",
        "periodic_refund_sweep",
        "periodic_refund_reconcile",
        "periodic_swap_reconcile",
        "periodic_routstr_fee_payout",
        "periodic_invoice_watcher",
    ):
        setattr(main, name, idle)
    import routstr.upstream.model_paths as paths

    paths.refresh_model_paths_periodically = idle

    real_run = main.run_analytics

    async def tracked_run() -> None:
        main.app.state.analytics_task = asyncio.current_task()
        await real_run()

    main.run_analytics = tracked_run

    @main.app.post("/__e2e/cancel-coordinator")
    async def cancel_coordinator() -> dict[str, bool]:
        task = main.app.state.analytics_task
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return {"collecting": terminal_outcomes.terminal_outcome_writer.running}

    @main.app.post("/__e2e/restart-collector")
    async def restart_collector() -> dict[str, bool]:
        writer = terminal_outcomes.terminal_outcome_writer
        await writer.stop()
        return {"collecting": await writer.start()}

    @main.app.get("/__e2e/ready")
    async def ready() -> dict[str, Any]:
        return {
            "ready": bool(proxy.get_upstreams()),
            "collecting": terminal_outcomes.terminal_outcome_writer.running,
            "run_id": terminal_outcomes.terminal_outcome_writer._run_id,
        }

    @main.app.post("/__e2e/reject-after-stop")
    async def reject_after_stop() -> None:
        await terminal_outcomes.terminal_outcome_writer.stop()
        terminal_outcomes.record_terminal_outcome(
            terminal_outcomes.TerminalOutcomeContext("forced-late", "e2e/model"),
            input_tokens=1,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            revenue_msats=0,
        )
        os._exit(0)  # No event-loop turn or graceful cleanup can persist evidence.

    # Test controls must precede the production catch-all proxy route.
    main.app.router.routes[:] = (
        main.app.router.routes[-4:] + main.app.router.routes[:-4]
    )
    await uvicorn.Server(
        uvicorn.Config(
            main.app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=20,
        )
    ).serve()


async def _wait_ready(
    client: httpx.AsyncClient, url: str, process: subprocess.Popen[bytes]
) -> None:
    async with asyncio.timeout(40):
        while True:
            assert process.poll() is None, f"worker exited: {process.returncode}"
            try:
                response = await client.get(url + "/__e2e/ready")
                if response.status_code == 200 and all(
                    response.json().get(k) for k in ("ready", "collecting")
                ):
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)


async def _exercise(url: str, work: Path) -> dict[str, Any]:
    import uvicorn
    import websockets
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from nostr_sdk import Event, Keys
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlmodel import col, select
    from sqlmodel.ext.asyncio.session import AsyncSession

    from routstr.core.db import (
        AnalyticsV2Outbox,
        ApiKey,
        TerminalOutcome,
        TerminalOutcomeEpoch,
        TerminalOutcomeWriterRun,
    )
    from routstr.nostr import analytics_v2_delivery as delivery

    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.connect() as connection:
        assert not await connection.run_sync(lambda c: inspect(c).get_table_names()), (
            "E2E requires EMPTY database"
        )
    ports = [_port() for _ in range(4)]
    upstream_port, relay_port, *worker_ports = ports
    upstream = FastAPI()
    started, release = asyncio.Event(), asyncio.Event()

    @upstream.post("/v1/chat/completions")
    async def completion(request: Request) -> Any:
        body = await request.json()
        if body["messages"][0]["content"] == "hold":
            started.set()
            await release.wait()
        result = {
            "id": "fixture",
            "object": "chat.completion",
            "created": 1,
            "model": "e2e/model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
            },
        }
        if body.get("stream"):

            async def chunks() -> Any:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": "fixture",
                            "model": "e2e/model",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "hello"},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                    + "\n\n"
                )
                yield "data: " + json.dumps({**result, "choices": []}) + "\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(chunks(), media_type="text/event-stream")
        return result

    server = uvicorn.Server(
        uvicorn.Config(
            upstream, host="127.0.0.1", port=upstream_port, log_level="warning"
        )
    )
    server_task = asyncio.create_task(server.serve())
    cert, key = work / "relay-cert.pem", work / "relay-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=analytics-relay.example.com",
            "-addext",
            "subjectAltName=DNS:analytics-relay.example.com",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    received: dict[str, dict[str, Any]] = {}

    async def relay(ws: Any) -> None:
        async for raw in ws:
            frame = json.loads(raw)
            if frame[0] == "EVENT":
                event = frame[1]
                assert Event.from_json(json.dumps(event)).verify()
                received[event["id"]] = event
                await ws.send(json.dumps(["OK", event["id"], True, "stored"]))
            elif frame[0] == "REQ":
                for event_id in frame[2]["ids"]:
                    if event_id in received:
                        await ws.send(
                            json.dumps(["EVENT", frame[1], received[event_id]])
                        )
                await ws.send(json.dumps(["EOSE", frame[1]]))

    (work / "migrations").symlink_to(ROOT / "migrations", target_is_directory=True)
    processes: list[subprocess.Popen[bytes]] = []
    logs: list[Any] = []
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "PYTHONPATH": str(ROOT),
        "NSEC": KEY,
        "ADMIN_PASSWORD": "local-e2e-only",
        "ENABLE_ANALYTICS_SHARING": "true",
        "PROVIDER_ID": "e2e",
        "RELAYS": '["wss://analytics-relay.invalid"]',
        "MODELS_REFRESH_INTERVAL_SECONDS": "0",
        "PROVIDERS_REFRESH_INTERVAL_SECONDS": "0",
        "SQLITE_IMPORT_PATH": "",
        "FIXED_PRICING": "false",
    }

    def launch(port: int) -> subprocess.Popen[bytes]:
        log = (work / f"worker-{port}-{len(logs)}.log").open("wb")
        logs.append(log)
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                str(port),
                str(upstream_port),
            ],
            cwd=work,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        processes.append(process)
        return process

    try:
        async with websockets.serve(relay, "127.0.0.1", relay_port, ssl=tls):
            async with httpx.AsyncClient(timeout=40) as client:
                first = launch(worker_ports[0])
                urls = [f"http://127.0.0.1:{port}" for port in worker_ports]
                await _wait_ready(client, urls[0], first)
                await delivery.claim_analytics_v2_identity(
                    sessions,
                    pubkey=Keys.parse(KEY).public_key().to_hex(),
                    provider_d="e2e",
                )
                await delivery.activate_analytics_v2_sharing(
                    sessions, coverage_day=datetime.now(UTC).date() - timedelta(days=1)
                )
                async with sessions() as session:
                    session.add(
                        ApiKey(
                            hashed_key="e2e-key",
                            balance=BALANCE,
                            refund_mint_url="http://unused.invalid",
                            refund_currency="sat",
                        )
                    )
                    # Test-clock setup: share today's period without waiting a day.
                    epoch = (
                        await session.exec(
                            select(TerminalOutcomeEpoch).where(
                                col(TerminalOutcomeEpoch.current_slot) == 1
                            )
                        )
                    ).one()
                    epoch.coverage_start_day = datetime.now(UTC).date()
                    await session.commit()
                second = launch(worker_ports[1])
                await _wait_ready(client, urls[1], second)

                async def request(
                    index: int, text: str = "hello", stream: bool = False
                ) -> httpx.Response:
                    response = await client.post(
                        urls[index] + "/v1/chat/completions",
                        headers={"Authorization": "Bearer sk-e2e-key"},
                        json={
                            "model": "e2e/model",
                            "messages": [{"role": "user", "content": text}],
                            "stream": stream,
                        },
                    )
                    assert response.status_code == 200, response.text
                    if stream:
                        assert "[DONE]" in response.text
                    return response

                await request(0)
                await request(1, stream=True)
                assert (
                    await client.post(urls[0] + "/__e2e/cancel-coordinator")
                ).json()["collecting"]
                await request(0)
                assert (await client.post(urls[1] + "/__e2e/restart-collector")).json()[
                    "collecting"
                ]
                await request(1)

                first_run = (await client.get(urls[0] + "/__e2e/ready")).json()[
                    "run_id"
                ]
                pending = asyncio.create_task(request(0, "hold"))
                await asyncio.wait_for(started.wait(), 10)
                first.terminate()
                await asyncio.sleep(0.2)
                assert first.poll() is None, (
                    "worker exited before draining in-flight HTTP"
                )
                release.set()
                await pending
                await asyncio.wait_for(asyncio.to_thread(first.wait), 25)
                assert first.returncode in (0, -signal.SIGTERM)
                async with sessions() as session:
                    stopped = await session.get(TerminalOutcomeWriterRun, first_run)
                    assert stopped is not None and stopped.status == "clean"
                first = launch(worker_ports[0])
                await _wait_ready(client, urls[0], first)
                await request(0)

                async def rows(count: int) -> list[TerminalOutcome]:
                    async with asyncio.timeout(10):
                        while True:
                            async with sessions() as session:
                                result = list(
                                    (await session.exec(select(TerminalOutcome))).all()
                                )
                            if len(result) == count:
                                return result
                            await asyncio.sleep(0.05)

                before_crash = await rows(6)
                second_run = (await client.get(urls[1] + "/__e2e/ready")).json()[
                    "run_id"
                ]
                second.kill()
                await asyncio.to_thread(second.wait)
                await asyncio.sleep(0.7)
                second = launch(worker_ports[1])
                await _wait_ready(client, urls[1], second)
                await request(1)
                async with sessions() as session:
                    abandoned = await session.get(TerminalOutcomeWriterRun, second_run)
                    assert abandoned is not None and abandoned.status == "recovered"
                    assert abandoned.loss_day is not None
                stored = await rows(7)
                assert (
                    len(before_crash) == 6
                    and len({row.outcome_id for row in stored}) == 7
                )
                async with sessions() as session:
                    key_row = await session.get(ApiKey, "e2e-key")
                    assert key_row is not None
                    assert BALANCE - key_row.balance == sum(
                        row.revenue_msats for row in stored
                    )
                    assert key_row.reserved_balance == 0
                    epochs = (await session.exec(select(TerminalOutcomeEpoch))).all()
                    assert len(epochs) == 1 and epochs[0].coverage_end_day is None

                with pytest.raises(httpx.RemoteProtocolError):
                    await client.post(urls[0] + "/__e2e/reject-after-stop")
                await asyncio.to_thread(first.wait)
                assert first.returncode == 0
                first = launch(worker_ports[0])
                await _wait_ready(client, urls[0], first)
                async with sessions() as session:
                    journaled = (
                        await session.exec(
                            select(TerminalOutcomeWriterRun).where(
                                col(TerminalOutcomeWriterRun.run_id).startswith("loss-")
                            )
                        )
                    ).all()
                    assert journaled and all(
                        run.loss_day is not None for run in journaled
                    )
                    after_rejection = set(
                        (await session.exec(select(TerminalOutcome.outcome_id))).all()
                    )
                    assert after_rejection == {row.outcome_id for row in stored}

                async def endpoint(relay_url: str) -> delivery.ResolvedRelayEndpoint:
                    return delivery.ResolvedRelayEndpoint(
                        "127.0.0.1", relay_port, "analytics-relay.example.com"
                    )

                async def no_limit(*args: Any) -> None:
                    return None

                real_connect = delivery.websockets.connect
                trusted = ssl.create_default_context(cafile=str(cert))

                def connect(*args: Any, **kwargs: Any) -> Any:
                    return real_connect(*args, ssl=trusted, **kwargs)

                # Local-only DNS and CA injection; real send/OK/exact-readback remains intact.
                old_resolver = delivery.resolve_public_relay_endpoint
                delivery.resolve_public_relay_endpoint = endpoint
                setattr(delivery.websockets, "connect", connect)
                try:
                    producer = delivery.AnalyticsV2Producer(
                        sessions,
                        private_key_hex=KEY,
                        public_key_hex=Keys.parse(KEY).public_key().to_hex(),
                        provider_d="e2e",
                    )
                    sender = delivery.AnalyticsV2Delivery(
                        sessions,
                        operator_relays=[
                            f"wss://analytics-relay.example.com:{relay_port}"
                        ],
                        relay_limit_reader=no_limit,
                    )
                    tomorrow = datetime.now(UTC) + timedelta(days=1)
                    assert await producer.produce_once(now=tomorrow) == 1
                    delivered = await sender.deliver_pending_once(
                        at_ms=int(tomorrow.timestamp() * 1000)
                    )
                    assert delivered.delivered_events == 1
                    assert len(received) == 1
                    prior_id = next(iter(received))
                    await request(1)
                    stored = await rows(8)
                    assert await producer.produce_once(now=tomorrow) == 1
                    delivered = await sender.deliver_pending_once(
                        at_ms=int(tomorrow.timestamp() * 1000)
                    )
                    assert delivered.delivered_events == 1
                    assert len(received) == 2
                    winner = sorted(
                        received.values(),
                        key=lambda event: (-event["created_at"], event["id"]),
                    )[0]
                    payload = json.loads(winner["content"])
                    assert payload["corrects"] == prior_id
                    day = datetime.now(UTC).date().isoformat()
                    expected = [
                        len(stored),
                        *[
                            sum(getattr(row, name) for row in stored)
                            for name in (
                                "input_tokens",
                                "output_tokens",
                                "cache_read_input_tokens",
                                "cache_creation_input_tokens",
                                "revenue_msats",
                            )
                        ],
                    ]
                    assert payload["days"][day] == expected
                    async with sessions() as session:
                        final_key = await session.get(ApiKey, "e2e-key")
                        assert final_key is not None
                        assert BALANCE - final_key.balance == expected[5]
                        assert final_key.reserved_balance == 0
                        outbox = (await session.exec(select(AnalyticsV2Outbox))).all()
                        assert all(
                            row.status in {"delivered", "superseded"} for row in outbox
                        )
                    return {
                        "requests": 8,
                        "workers": 2,
                        "ledger_vector": expected,
                        "signed_relay_events": 2,
                        "late_correction": True,
                        "sigterm_drained": True,
                        "sigkill_restart": True,
                        "stale_lease_recovered": True,
                        "coordinator_cancellation": True,
                        "collector_restart": True,
                        "post_shutdown_rejection_durable": True,
                    }
                finally:
                    delivery.resolve_public_relay_endpoint = old_resolver
                    setattr(delivery.websockets, "connect", real_connect)
    finally:
        release.set()
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            if process.poll() is None:
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait), 25)
                except TimeoutError:
                    process.kill()
                    await asyncio.to_thread(process.wait)
        for log in logs:
            log.close()
        server.should_exit = True
        await server_task
        await engine.dispose()


@pytest.mark.asyncio
async def test_analytics_publication_e2e(tmp_path: Path) -> None:
    url = os.environ.get("ROUTSTR_E2E_POSTGRES_URL")
    if not url:
        pytest.skip(
            "Set ROUTSTR_E2E_POSTGRES_URL to an EMPTY disposable PostgreSQL database"
        )
    evidence = await _exercise(url, tmp_path)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    if sys.argv[1] == "--worker":
        asyncio.run(_worker(int(sys.argv[2]), int(sys.argv[3])))
