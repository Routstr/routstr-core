# Current main: real-network streaming reservation reproductions

## Tested version and environment

- Commit: `96c8e2f77de8e9f8a0979d17dba0a6d20c78fe89` (local main at investigation time; no remote fetch was performed).
- Unpatched application built using its Dockerfile and frozen lockfile.
- Image: `localhost/routstr-reserved-repro:main`, ID `a787e603f565f3d34e1cc3999793d9dc2d2e3c968eb0ce0ded2f485450719bd0`.
- Podman 5.8.4; Python 3.14; Starlette 1.6.0; Uvicorn 0.31.1.
- Loopback ports 18090 (router), 18091 (dummy upstream), 18092 (diagnostic control).
- Separate container-local SQLite databases and synthetic balances; no original node data or secrets mounted.
- Read timeout accelerated to 3 seconds (confirmed effective); lease expiry to 6 seconds; heartbeat every 2 seconds. Background sweep remains 60 seconds.

## Results

| Scenario | Result |
| --- | --- |
| Finite stream with usage and DONE | Charged normally, zero reservation |
| One chunk then silence, client connected | Read timeout fired, estimated usage charged, zero reservation |
| One chunk then silence, client disconnected after 1 second | Reservation cleared on upstream read timeout; prompt disconnect cleanup was not demonstrated |
| No upstream response headers | Timeout produced HTTP 424; reservation released |
| Endless content stream, client disconnected after 1 second | Continued renewing; exact refund HTTP 400 persisted across background sweep |
| SSE comment-only keepalives every 0.5 seconds | No meaningful content or completion, but lease renewed and refund blocked; remained active after client disconnected |
| Flood stream to client that never reads | Lease renewed while client was stalled; still renewed after client socket closed |

The three problematic streams retained 11-msat reservations through the full observation window. They began at timestamp 1790766366; at 1790766635, all remained active with lease timestamps 1790766634. Thus renewal continued for roughly 269 seconds, far beyond the 3-second read timeout, 6-second lease timeout, and multiple 60-second sweep intervals. All test clients were gone by approximately 1790766439.

This proves persistence for minutes, not a measured days-long run. No new inference requests were made for the keys during observation; refund probes did not renew the leases.

The flood scenario sends 64-KiB content deltas rapidly and uses a 1-KiB client receive buffer. It exercises a real non-reading downstream socket, but no live task-stack capture was collected to establish the precise blocked await at each snapshot.

## Why the newer timeout is insufficient

The read timeout is an inactivity timeout for upstream reads. Endless content or SSE keepalive bytes avoid it. A downstream-send wait is not bounded by it.

More importantly, the runtime did not reliably propagate downstream disconnect into termination of these streams. Closed clients left upstream connections established and reservation owners alive, so heartbeats kept making the durable rows fresh. The sweeper therefore correctly declined to release them under its current policy.

## Framework evidence and diagnostic control

Captured sources (`starlette-source.txt`, `uvicorn-source.txt`) show:

- Uvicorn 0.31.1's httptools protocol advertises ASGI HTTP spec 2.4.
- Its `send()` returns silently when `self.disconnected` is true; it does not raise an OSError.
- Starlette's StreamingResponse for ASGI >=2.4 relies on a send OSError to signal client disconnect, rather than running its older explicit disconnect listener.
- BaseHTTPMiddleware's outer streaming wrapper also does not explicitly listen for disconnect.

This is a concrete framework compatibility concern consistent with the observations. Deterministic confirmation via a server-version/spec comparison or task instrumentation remains future work.

A diagnostic second router removed only LoggingMiddleware using `no_logging_app.py`. Endless and keepalive clients still left active reservations after disconnect (`control-results.txt`). Thus LoggingMiddleware alone is not sufficient to explain the disconnect leak in this environment. This control is not a proposed production patch.

When the dummy upstream was forcibly stopped, the control router finalized both streams. The unmodified main router still showed the three reservations active five seconds afterward and subsequently needed SIGKILL after a ten-second shutdown grace period. Logs showed upstream termination warnings but no completed settlement for those three in the captured window. The exact finalization blockage was not traced; it should be investigated separately, potentially including middleware delivery/backpressure interactions. Do not assert that upstream termination always clears these main reservations.

## Reproduce

From project root:

```bash
podman build --build-arg GIT_COMMIT=$(git rev-parse HEAD) --build-arg GIT_TAG=main \
  -t localhost/routstr-reserved-repro:main .

podman run -d --name reserved-dummy-main --network host \
  -v "$PWD/reservation-repro-main:/repro:ro,Z" \
  --entrypoint /.venv/bin/python localhost/routstr-reserved-repro:main \
  -m uvicorn dummy_upstream:app --app-dir /repro --host 127.0.0.1 --port 18091

podman run -d --name reserved-router-main --network host \
  -e DATABASE_URL=sqlite+aiosqlite:////tmp/reserved-main.db \
  -e UPSTREAM_BASE_URL=http://127.0.0.1:18091/v1 -e UPSTREAM_API_KEY=dummy \
  -e STALE_RESERVATION_TIMEOUT_SECONDS=6 -e UPSTREAM_READ_TIMEOUT=3 \
  -e CASHU_MINTS= -e ENABLE_PRICING_REFRESH=false \
  -e MODELS_REFRESH_INTERVAL_SECONDS=0 -e ADMIN_PASSWORD=local-repro-only \
  --entrypoint /.venv/bin/python localhost/routstr-reserved-repro:main \
  -m uvicorn routstr.core.main:app --host 127.0.0.1 --port 18090
```

Wait for application startup and verify `/v1/models` includes gpt-4o-mini. Model/pricing discovery uses external services; this is not fully offline.

```bash
podman exec -i reserved-router-main /.venv/bin/python - <<'PY'
import asyncio
from routstr.core.db import ApiKey, create_session
async def main():
    async with create_session() as s:
        for k in ['finite','silent','silent-disconnect','endless-disconnect','keepalive','flood','header']:
            s.add(ApiKey(hashed_key='main-'+k, balance=1000000000))
        await s.commit()
asyncio.run(main())
PY

.venv/bin/python reservation-repro-main/probe.py
```

The probe runs approximately 80 seconds, snapshots the DB, attempts refunds only on reserved keys (not actual Cashu payouts), and closes all clients. Later DB snapshots show continued renewal. Use fresh container names/databases on repeats or deliberately remove only the retained reproduction containers first. Do not overwrite original node containers.

## Evidence and remaining work

- `results.txt`: scenario matrix snapshots and refund errors.
- `connections.txt`: upstream sockets remained after downstream sockets disappeared.
- `final-before-stop.json`: continued renewal roughly 269 seconds after start.
- `after-upstream-stop.json`: reservations still active in unmodified main five seconds after upstream termination.
- `router.log`, `upstream.log`: application evidence before router shutdown.
- `control-results.txt`, `control-router.log`: comparison without LoggingMiddleware.
- `starlette-source.txt`, `uvicorn-source.txt`: installed framework behavior.

Need: real-network regression tests, framework compatibility correction/verification, explicit disconnect monitoring that reaches upstream ownership, bounded downstream delivery, total request lifetime, and task-stack diagnostics for finalization stalls. Database fault injection, restart/multi-worker behavior, and alternate API routes were not tested here.

All three main reproduction containers were stopped. The unmodified main router required SIGKILL; its retained database may contain active reservations. No application source fixes were made.
