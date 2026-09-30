# Reserved balance blocks refunds long after the last request

## Reported issue

A client attempting to refund an API key receives:

> Cannot refund key. There are ongoing requests for this api key.

The user reports that the key has not been used in a very long time, potentially days. This is not a refund racing with normal request completion. The expected behavior is that reservations left by disconnected, crashed, abandoned, or failed requests eventually expire and the key becomes refundable.

The error does **not** prove that an upstream inference request is running. In the current implementation, it means the refund endpoint still sees a positive aggregate `reserved_balance` after attempting stale-reservation cleanup.

This document records a source-code investigation of the current checkout. The affected node's database, logs, runtime tasks, effective configuration, and deployed version have not been inspected. The production root cause remains unconfirmed.

## Investigation scope and results

Checkout inspected: `96c8e2f7` (`Merge pull request #790 from Routstr/fix/rename-unsupported-param`).

The existing cleanup system is implemented and wired into application startup. It protects several important accounting invariants, but it is based on renewable reservation leases rather than a hard maximum request lifetime.

Verification command:

```bash
.venv/bin/pytest \
  tests/unit/test_stale_reservations.py \
  tests/unit/test_streaming_billing_finalization.py \
  tests/integration/test_negative_available_balance_repro.py -q
```

Result: **59 passed in 10.04 seconds**.

These passing tests verify existing recovery paths; they do not establish what happened on the affected node or demonstrate recovery from every kind of live-but-hung task. No implementation changes were made during this investigation.

## Reservation lifecycle

### 1. Reserve before forwarding

`pay_for_request()` in `routstr/auth.py` reserves funds before dispatching the billed request upstream.

It creates a durable `ReservationRelease` identity containing:

- `id`: the individual reservation identity;
- `key_hash`: the request's key;
- `billing_key_hash`: the key whose balance backs the request;
- `reserved_msats`: the amount owned by this reservation;
- `status`: initially `active`;
- `created_at`: initially the current timestamp.

The aggregate reserved balance and durable reservation row commit together. The request's reservation identity matters: releasing one request must not erase funds reserved by another concurrent request.

`ApiKey.reserved_at` is also stamped when funds are reserved. It is an aggregate timestamp, not an independent timestamp for each request.

### 2. Renew while the owner task remains alive

`_start_reservation_heartbeat()` in `routstr/auth.py` starts a task for each reservation. Its interval is:

```python
max(1, settings.stale_reservation_timeout_seconds // 3)
```

With the default timeout of 300 seconds, renewal occurs approximately every 100 seconds.

The heartbeat captures `asyncio.current_task()` as the owner. At each iteration it checks:

```python
if owner is None or owner.done():
    return
```

If the owner is still alive, it calls `renew_reservation()` using a separate database session. Renewal updates the active durable row's `created_at` to the current time.

Important consequences:

- Renewal depends on task lifetime, not demonstrated request progress.
- There is no original-age limit in this heartbeat.
- `created_at` is overwritten, so it actually serves as a renewable lease timestamp.
- An owner that has finished cannot keep renewing indefinitely through this heartbeat.
- An owner that is blocked indefinitely may keep renewing indefinitely.

### 3. Settle or release

Normal completion settles the charge and releases the reservation. Handled upstream failures revert the reservation. Terminal reservation transitions stop the heartbeat.

The proxy includes cancellation cleanup. Streaming paths use finalizers and ownership wrappers to improve cleanup across cancellation and downstream-send failures. Relevant code includes:

- `routstr/auth.py`;
- `routstr/proxy.py`;
- `routstr/upstream/base.py`;
- `routstr/upstream/stream_ownership.py`.

If a request dies without completing cleanup, its heartbeat is intended to stop once the owning task is done. The reservation can then age out and be released by the sweeper.

## Existing cleanup mechanisms

### Background sweep

`periodic_stale_reservation_sweep()` in `routstr/auth.py` is started by the application lifespan in `routstr/core/main.py`.

Defaults:

| Setting/mechanism | Default | Meaning |
| --- | --- | --- |
| `STALE_RESERVATION_TIMEOUT_SECONDS` | 300 seconds | Maximum age of an unrenewed reservation lease before it is stale |
| `STALE_RESERVATION_SWEEP_INTERVAL_SECONDS` | 60 seconds | Interval between background cleanup passes |
| Heartbeat interval | 100 seconds | Approximately one third of the stale timeout |
| `UPSTREAM_READ_TIMEOUT` | 900 seconds | Upstream HTTP read inactivity timeout, not a total request deadline |
| `RESET_RESERVED_BALANCE_ON_STARTUP` | `True` | Explicit startup reset of active reservations and aggregate reserved balances |

The sweeper calls `release_stale_reservations()` in `routstr/core/db.py`.

For durable reservations, it selects `active` rows whose `created_at` is older than the cutoff. Its terminal update also checks the timestamp, protecting against a heartbeat that renews between selection and release.

Each successful release subtracts that reservation's own amount from the relevant aggregates. Healthy releases commit individually so that certain later corruption repairs cannot roll them back.

Under healthy execution, recovery occurs after the last lease renewal has aged beyond the configured timeout, plus sweep scheduling and database-operation time. This is **not** a guarantee of release 300 seconds after the request originally began.

### Refund-time cleanup

`refund_wallet_endpoint()` in `routstr/balance.py` checks for reserved funds before opening the refund claim.

If `key.reserved_balance > 0`, it:

1. Calls `release_stale_reservations()` scoped to that key.
2. Refreshes the key from the database.
3. Returns HTTP 400 with the reported message if reserved balance remains.

Thus, the current refund path does not rely exclusively on the background task having run. A stale durable reservation should also be releasable during refund itself.

If cleanup raises an unexpected exception instead, that is a separate failure from this specific HTTP 400 branch.

### Legacy aggregate cleanup

Older deployments may have aggregate reserved balances without matching durable rows.

The cleanup function also looks for these legacy aggregates, but only clears them when there is no active durable owner. It uses a compare-and-swap guard on the observed balance and timestamp to avoid erasing a newly created reservation.

The behavior differs between background and targeted cleanup:

| Legacy aggregate state, with no active durable owner | Background sweep | Refund-time targeted cleanup |
| --- | --- | --- |
| Old `reserved_at` | Eligible for release | Eligible for release |
| Recent `reserved_at` | Preserved | Preserved |
| `reserved_at = NULL` | Deliberately skipped | Eligible for repair |

The NULL-timestamp behavior is explicitly covered by existing tests. It is a background-recovery limitation, but **alone it does not explain the reported refund rejection on the current checkout**, because targeted refund cleanup heals it.

### Startup reset

When enabled, startup calls `reset_all_reserved_balances()`. It marks active durable reservations released and clears aggregate reserved balances and timestamps.

This is not a safe universal operational fix. In a shared-database, multi-instance setup, another instance may still own a legitimate in-flight request. Resetting its reservation can break billing. The setting's source comment recommends disabling it for horizontal scaling.

## Why the 900-second HTTP timeout does not guarantee eventual completion

The user correctly asks: if the last request was days ago, shouldn't a 900-second upstream timeout have completed or failed the request long before now?

**For an ordinary request actively waiting for upstream bytes, with no bytes arriving, yes.** It should hit the read timeout and reach failure cleanup. A days-long refund blockage is abnormal, not expected behavior for a silent upstream.

However, the HTTP read timeout is not an absolute deadline spanning the complete request lifecycle.

### Upstream continues sending bytes

A stream can avoid a read inactivity timeout by delivering bytes periodically. Those bytes might be content or keepalive traffic. A stream with no total-duration limit could therefore remain open longer than 900 seconds.

This is a technical possibility, **not evidence that the affected upstream streamed for days**. It must not be assumed as the production explanation.

### Router is blocked writing to the downstream client

If the router has received a chunk and is blocked delivering it to the client, it may not currently be waiting on an upstream HTTP read. The upstream read timeout is not a general bound on downstream ASGI sends.

Whether a particular blocked send keeps the captured owner task alive depends on the execution path. That behavior needs a runtime trace or regression test, rather than an assumption about all stream paths.

### Router is blocked after upstream completion

Database settlement, finalization, or resource cleanup happens outside the upstream read operation. The upstream read timeout does not bound these waits.

If the heartbeat's owning task remains alive while waiting, renewal may continue. If that owner finishes and only detached cleanup remains, the heartbeat should stop and the sweeper should eventually recover the reservation.

### Conclusion

The current code has no common hard lifetime limit found in this investigation that covers reservation creation, upstream dispatch, streaming delivery, and finalization together.

The missing guarantee is:

> A live-but-stuck request cannot renew its reservation forever.

This gap is confirmed by the heartbeat's renewal condition. The specific blocked operation, if any, on the affected node is not known.

## Findings and hypotheses

### Confirmed: renewal does not require progress

An owner task being alive is sufficient to renew the lease. Neither original request age nor meaningful progress is checked.

This permits indefinite reservation retention in principle, even without new requests using the key.

### Confirmed: immutable request age is not stored in the reservation row

`ReservationRelease.created_at` doubles as the last-renewal timestamp. Once renewed, it cannot tell us when the request originally started.

This impairs diagnostics and prevents enforcing an original-age limit from this field alone.

### Confirmed: NULL legacy timestamps are not background-cleaned

Such keys may remain reserved indefinitely in the background. The current refund endpoint has targeted recovery for this state, subject to the absence of an active durable owner.

### Confirmed: unexpected failures can interrupt a sweep pass

The background loop catches unexpected exceptions, logs `Error in periodic_stale_reservation_sweep`, and retries after the sweep interval.

Some aggregate-corruption cases are handled per reservation, but not every database exception is isolated per record. A persistently failing operation could repeatedly interrupt a pass. Whether this prevents a particular key's cleanup depends on the failure and processing order.

There is no evidence yet that this caused the reported error.

### Possible: affected deployment differs from this checkout

The current code includes heartbeat-owner binding, targeted legacy recovery, and corruption handling. The affected node may run older or different code.

The deployed commit must be established before treating local behavior as proof of production behavior.

### Possible: future timestamps or unusual effective configuration

A future-dated lease can remain non-stale unexpectedly. An unusually large configured timeout can also preserve old reservations.

Clock skew between instances sharing a database can affect lease timestamps and age calculations. These are diagnostic checks, not confirmed causes.

## Existing verified recovery coverage

The suites run during this investigation cover, among other cases:

- Stamping aggregate reservation timestamps on payment.
- Reverting individual reservations without erasing siblings.
- Releasing old reservations and preserving fresh ones.
- Resetting reserved balances during explicit startup reset.
- Refund-time recovery of stale and legacy NULL-timestamp aggregates.
- Refusing refunds while a recent reservation remains.
- Streaming finalization and client-disconnect cleanup.
- Owner task termination allowing recovery of an abandoned reservation.
- Lease renewal across an in-flight request.
- Renewal racing with stale release.
- Legacy aggregate release racing with a new reservation.
- Several accounting-corruption cases and safe terminal repair.
- Preventing late charges after a reservation has reached a released terminal state.

These tests do not substitute for explicit tests of endless keepalive streams, blocked downstream sends, or finalization that never completes.

## Production diagnosis: distinguish a renewing lease from failed cleanup

The most useful initial question is:

> Is the reservation still being renewed, or is it stale and not being released?

Do not share the raw API-key secret. Use its stored hash and reservation identifiers in restricted operational diagnostics.

### 1. Establish deployment and configuration

Record:

- Deployed commit/version.
- Effective `STALE_RESERVATION_TIMEOUT_SECONDS`.
- Effective `UPSTREAM_READ_TIMEOUT`.
- Startup-reset setting.
- Number of instances sharing the database.
- Current time on each relevant instance.
- Whether the lifespan/background tasks completed startup.

Use effective settings, not only environment variables; settings initialization includes persisted configuration.

### 2. Inspect the key and all related reservations

Read-only queries:

```sql
SELECT hashed_key, balance, reserved_balance, reserved_at
FROM api_keys
WHERE hashed_key = :key_hash;

SELECT id, key_hash, billing_key_hash,
       reserved_msats, status, created_at
FROM reservation_releases
WHERE key_hash = :key_hash
   OR billing_key_hash = :key_hash;
```

Inspect both key relationships, since a reservation may reference the key as request owner or billing owner.

Take two snapshots approximately 110 seconds apart with default settings, or use an interval longer than the effective heartbeat interval. A pair of snapshots is a useful signal; it is not a substitute for longer observation when renewal is delayed or intermittent.

### 3. Interpret the results

| Observation | Investigation direction |
| --- | --- |
| Active reservation timestamp advances | Identify the instance and owning task renewing it; inspect its stack and actual progress |
| Active reservation timestamp is older than the stale cutoff and does not advance | Check sweep execution/errors, refund cleanup, deployed code, and accounting state |
| Reserved balance remains with no active durable rows | Inspect legacy timestamp and aggregate recovery; current targeted refund cleanup should repair stale/NULL state |
| Lease timestamp is in the future | Check clocks and timestamp integrity |
| Some rows are stale and others fresh | Release only stale owners; do not clear the whole key |
| Aggregate amount disagrees with active durable ownership | Investigate accounting drift and safe reconciliation |

If the lease is genuinely days old and unrenewed, the indefinite-heartbeat explanation does **not** explain that row. Cleanup failure or incompatible deployment becomes the relevant direction.

### 4. Inspect logs and task state

Relevant existing log messages include:

- `Error in periodic_stale_reservation_sweep`.
- `Failed to renew billing reservation lease`.
- `Released stale reservations`.
- `Released corrupt stale reservation without aggregate subtraction`.
- `Released corrupt reservation without aggregate subtraction`.
- `Client disconnected mid-request, reverting reservation`.
- `refund_wallet_endpoint: released stale reservation before refund`.

For a renewing lease, locate the process with that reservation's heartbeat and inspect the owner's stack. Determine whether it is waiting on upstream input, downstream delivery, database work, finalization, or another operation.

Also correlate the original request with upstream outcome and billing logs. A heartbeat alone does not demonstrate that inference is still running.

## Proposed hardening

These are proposed changes, not completed fixes.

### 1. Separate original age from renewable lease age

Keep distinct durable fields for:

- Immutable reservation/request start time.
- Last lease renewal time.

Consider additional progress and ownership metadata where justified. Define migration behavior explicitly: existing renewed `created_at` values cannot reconstruct true original start times.

### 2. Bound the actual request, not just the accounting lease

Introduce a configurable total billed-request lifetime covering all relevant routes and phases, including streaming delivery. Add appropriate inactivity bounds for upstream waits and downstream delivery, and bounded finalization/cleanup behavior.

Timeout handling should:

1. Stop or cancel the owning request and close owned resources.
2. Settle known or estimated delivered usage according to existing billing policy.
3. Release only that request's remaining reservation.
4. Stop heartbeat renewal.
5. Reach a durable terminal state that prevents later charging.

Do **not** merely stop renewal or zero the key while a request continues running. Releasing funds while upstream work can still finish creates refund/late-charge and provider-cost risks.

Care is also needed not to cancel legitimate long-running inference accidentally. Request lifetime, inactivity, and lease expiry are different concepts and should have distinct documented policies.

### 3. Improve stalled-owner detection and observability

Expose actionable, non-secret diagnostics:

- Reservation identity and owning instance.
- Immutable age and current lease age.
- Last meaningful progress and current phase, if tracked.
- Reason for terminal transition or refused refund.
- Age and count of active reservations.
- Sweep failures and cleanup duration.

Do not treat upstream keepalive bytes as necessarily meaningful model progress. Decide deliberately which signals should extend which deadlines.

### 4. Reconcile legacy and inconsistent aggregates safely

Define a migration/recovery policy for NULL legacy timestamps, rather than leaving them background-ineligible indefinitely.

Mixed-version deployments require caution: an aggregate without a durable row might still belong to an older live worker. Any reconciliation must preserve valid durable owners and avoid unsafe whole-key resets.

Investigate positive residual aggregates even after durable rows become terminal, with concurrency guards and accounting invariants preserved.

### 5. Make cleanup failures diagnosable and resilient

Consider bounded database operations, per-record failure isolation where safe, and alerts for repeated sweep failures or reservations exceeding expected age.

Failure isolation must not weaken atomicity between durable transitions and aggregate updates. A failed release must not partially debit unrelated reservations.

## Regression tests needed to close the gaps

Add tests that reproduce and verify recovery for:

1. A live owner waiting indefinitely without progress.
2. An endless upstream stream sending keepalive bytes below the read-timeout interval.
3. A downstream send blocked indefinitely after receiving an upstream chunk.
4. Finalization or database settlement that stalls.
5. Cancellation before streaming begins, during streaming, and during finalization.
6. Renewing lease older than the new maximum original-age limit.
7. Background legacy NULL-timestamp recovery under the chosen migration policy.
8. Corrupt residual aggregates alongside a healthy active sibling reservation.
9. A failing cleanup operation followed by other recoverable reservations.
10. Multiple workers concurrently renewing, sweeping, timing out, and refunding.
11. Late completion attempting to charge after timeout/release.
12. Future lease timestamps and the chosen clock-skew policy.

For each timeout/recovery test, assert:

- The underlying request/resource is stopped or closed as intended.
- No heartbeat can renew indefinitely afterward.
- Only the affected reservation is released.
- Sibling reservations remain intact.
- Balance/reserved accounting remains valid.
- Terminal transitions are idempotent.
- A later completion cannot charge released/refunded funds.
- The key becomes refundable when no legitimate reservations remain.

## Operational caution

Do not solve the symptom by manually setting `reserved_balance = 0` while active requests or heartbeat tasks may exist. Durable reservation state and aggregate balances must agree, and late completion must not be allowed to spend refunded funds.

Any production repair should begin with a read-only snapshot and identification of live ownership, then use an accounting-safe terminal transition or controlled maintenance procedure.

## Bottom line

The expected stale cleanup exists. A genuinely dead, unrenewed reservation should recover on the current version with healthy database access, including during a refund attempt.

The confirmed design gap is that **a task remaining alive is sufficient to renew its reservation indefinitely**, and the upstream 900-second read timeout does not bound every phase of that task's lifetime.

A days-old refund blockage therefore warrants investigation, not an assumption that normal request processing is still underway. The first decisive evidence is whether the affected reservation's lease timestamp continues advancing. The production root cause and implementation fixes remain open.

## Release-specific reproduction: v0.4.7 (confirmed)

The user subsequently confirmed that the affected node runs the released **v0.4.7** tag. Testing that tag revealed an important correction to the initial analysis above:

**The 900-second upstream read timeout exists in the newer checkout, not in v0.4.7.** The release's forwarding paths construct `httpx.AsyncClient(..., timeout=None)`. It has no `upstream_read_timeout` settings field. Setting `UPSTREAM_READ_TIMEOUT=3` in the reproduction did nothing; importing the release settings confirmed the field is absent.

Therefore, on this release an upstream can send one chunk and then remain completely silent without triggering an HTTP read timeout. Periodic bytes are not needed to explain indefinite waiting.

### Environment and isolation

- Podman: 5.8.4, netavark network backend.
- Release commit: `f32565e2547abbbffd77a01198ef683ecb8e3d4f`.
- Detached worktree: `.worktrees/reserved-balance-v047`.
- Built the release's own Dockerfile (Python 3.11 base), without source patches.
- Image: `localhost/routstr-reserved-repro:v0.4.7`.
- Image ID: `8c3340a33040df37439f7085e369050d3acc2adf0324bc024e1b4288a3601f76`.
- Separate containers, loopback ports 18080/18081, container-local SQLite database.
- No original node database, wallet, secrets, volumes, or image tag were changed.
- Host networking avoided the reported aardvark DNS issue for this experiment; containerized DNS was not tested or repaired.
- Accelerated stale timeout: 6 seconds, heartbeat every 2 seconds. Background sweep retained its actual 60-second interval.
- Synthetic database-funded keys avoided introducing Cashu mint behavior into the reservation test. Actual refund payout success was not tested.

### Dummy upstream scenarios

A small local OpenAI-compatible server exposed `/v1/models` and `/v1/chat/completions` using `gpt-4o-mini`:

1. **Finite:** three chunks, a usage event, and `[DONE]`.
2. **Silent:** one chunk, then sleep for 3600 seconds.
3. **Endless:** a content chunk every 0.5 seconds with no terminal event.

The test client consumed streams, queried reservation state, attempted refunds, and disconnected. Evidence and reusable scripts are in `reservation-repro-v047/`.

### Observed results

| Scenario | Outcome |
| --- | --- |
| Finite stream | Settled normally; reserved balance became zero |
| Silent stream | Did not time out; durable lease kept renewing |
| Endless stream | Lease kept renewing; refund returned the exact reported HTTP 400 |
| Both clients disconnected | Both upstream connections remained established; both reservations remained active and kept renewing |
| After more than a background-sweep interval | The abandoned reservations were still active; their fresh leases prevented stale cleanup |
| Dummy upstream forcibly stopped | Both requests finally reached error/finalization; both reserved balances became zero and rows became `charged` |

Both streams reserved 11 msats. Their lease timestamps initially advanced from `1790765677` through `1790765685` and `1790765695`. After client termination, a later snapshot at `1790765791` still showed both rows `active` with leases at `1790765789`. This is approximately 116 seconds after their creation and well beyond the accelerated stale timeout and a background-sweep interval.

At that later point, `ss` showed two established router-to-upstream connections and no test-client connection on port 18080. A refund for the silent key still returned:

```json
{"detail":"Cannot refund key. There are ongoing requests for this api key."}
```

Stopping the dummy upstream broke those connections. Finalization then charged estimated usage and cleared the reservations. The finite and silent keys ended with a 3-msat charge; the endless stream accumulated a 25-msat charge. This also demonstrates that abandoned upstream work can continue affecting billing after the downstream client is gone.

The first probe run ended with a client-side `TimeoutError` because it expected the silent stream to complete. That timeout was imposed by the probe's `asyncio.wait_for`, not by the router. The saved probe was subsequently adjusted to report this expected observation rather than crash.

### What this establishes

We have reproduced a plausible mechanism for a key remaining blocked long after the client last used it on **the exact release tag**:

1. The upstream stream remains open, even silently.
2. Downstream disconnection does not terminate the upstream-owning request in the tested runtime/path.
3. The owner remains alive, so its heartbeat keeps renewing.
4. Background and refund-time stale cleanup preserve the fresh lease.
5. Refund remains blocked indefinitely unless the upstream closes or another intervention stops the owning work.

The reproduction lasted minutes, not days. The absence of a read timeout and continuing renewal explain how the state can persist longer; no days-long run was performed.

This is concrete release-specific evidence, but not proof that the affected production key has this exact state. Production confirmation still requires reservation snapshots and logs.

### Shutdown symptoms

The dummy upstream also needed SIGKILL after a short SIGTERM grace period while its streams were open. The router stopped normally after the upstream was stopped and its streams finalized.

This supports the possibility that outstanding streaming work can delay graceful shutdown. It does not establish that the user's earlier router/UI shutdown warnings share the same cause. The aardvark DNS removal failure is a separate Podman networking symptom; the reproduction does not require it.

### Next implementation work

Prioritize fixes/backports appropriate to v0.4.7:

- Finite upstream transport timeouts, including reads and header waits.
- Reliable downstream-disconnect propagation and deterministic closure/finalization of owned streaming resources in the deployed FastAPI/Starlette/Uvicorn combination.
- A maximum request lifetime independent of renewable leases and keepalive bytes.
- Real-network regression tests that disconnect a client from a silent upstream stream and assert upstream closure, terminal billing state, stopped renewal, and zero residual reservation.

The newer checkout has transport timeout and stream-ownership changes, but this experiment did not validate the same scenario against that newer checkout. Do not assume an upgrade fully fixes every gap without rerunning the reproduction.

Both reproduction containers were stopped at the end. Their container-local database and logs were retained for inspection; no original services were restarted.

## Current main reproduction: timeout does not close every gap

The same investigation was repeated against unpatched local main commit `96c8e2f77de8e9f8a0979d17dba0a6d20c78fe89` using its own Dockerfile and frozen dependencies. The main image ran Python 3.14, Starlette 1.6.0, and Uvicorn 0.31.1. Detailed commands and evidence are in `reservation-repro-main/README.md`.

With an effective upstream read timeout of 3 seconds and stale timeout of 6 seconds:

- Finite completion settled correctly.
- Silent upstream streams reached the read timeout and cleared reservations.
- A header wait timed out with HTTP 424 and released its reservation.
- An endless content stream **after client disconnect** kept renewing and returning the reported refund HTTP 400.
- SSE comment-only keepalives evaded read timeout; renewal persisted even after disconnect.
- A flood stream to a downstream client that never read remained reserved, including after its socket closed.

The three problematic keys remained active approximately 269 seconds after request start, across multiple background sweeps, with fresh lease timestamps. This is not merely an active client asking for a refund: all downstream test clients were gone well before the final observation.

### Framework compatibility concern

Installed framework source provides a specific lead:

- Uvicorn's httptools protocol advertises ASGI HTTP 2.4.
- Its send function silently returns after downstream disconnection.
- Starlette's ASGI >=2.4 StreamingResponse path expects send to raise OSError for disconnect detection and does not run the older disconnect listener.

This mismatch is consistent with streams continuing to consume upstream bytes while downstream sends become no-ops. Captured code is in the evidence directory. A runtime task-stack or controlled framework-version comparison is still needed for complete causal validation.

Removing only LoggingMiddleware in a diagnostic router did not resolve disconnect renewal. Therefore, do not attribute the disconnect problem solely to that middleware.

### Additional finalization/shutdown observation

Forcibly stopping the dummy upstream finalized the diagnostic router's streams, but the unmodified router still had active reservations five seconds after upstream termination and required SIGKILL after a ten-second SIGTERM grace period. Its logs showed upstream termination warnings without completed settlement for those three requests in the captured window. The precise blocked operation was not traced.

This adds a finalization/delivery investigation beyond transport inactivity. In this main reproduction, unlike the release reproduction, upstream termination did not promptly clear every reservation.

### Updated conclusion

The newer read timeout fixes silent upstream waits, but **does not eliminate reservation leaks for disconnected clients whose upstream streams keep producing bytes, or stalled downstream delivery**. The stream ownership/finalizer unit tests previously run do not exercise the complete real server/framework/middleware network path that exposed these cases.

Prioritize real-network regression coverage and disconnect propagation, the installed server/framework compatibility, bounded downstream delivery and finalization, and an absolute request lifetime independent of keepalive traffic. No implementation fix has been made; alternate routes, multi-worker behavior, and database fault injection remain untested.
