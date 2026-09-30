# Reservation lifecycle implementation and validation

Branch: fix/reservation-lifecycle. Baseline: 96c8e2f7.

## Implemented

- Outermost pure-ASGI lifecycle supervision with one coordinated receive consumer, explicit disconnect monitoring, cancellation, and exact reservation fallback cleanup.
- Finite overall request lifetime (MAX_REQUEST_LIFETIME_SECONDS, default 1800), downstream send timeout (DOWNSTREAM_SEND_TIMEOUT_SECONDS, default 60), and cleanup timeout (REQUEST_CLEANUP_TIMEOUT_SECONDS, default 30).
- Lifecycle identity shared through context across middleware tasks; reservation replacements are registered for exact cleanup.
- Heartbeats stop on lifecycle termination or local maximum age.
- Persistent stream finalization has a finite cleanup budget.
- Durable immutable started_at and expires_at columns; expiry covers remaining request lifetime plus settlement grace, including provider fallback without restarting the original deadline.
- Renewal and charge claims refuse expired reservations. Sweeping can release absolute-expired reservations even when their renewable timestamp is fresh.
- Migration grants legacy active rows 1830 seconds of grace; original ages are not fabricated. Drain old workers before deployment.

## Verification

Run from worktree with PYTHONPATH=$PWD because the shared root virtual environment's editable install points at the original checkout:

PYTHONPATH=$PWD ../../.venv/bin/pytest tests/unit/test_request_lifecycle.py tests/unit/test_stale_reservations.py tests/unit/test_streaming_billing_finalization.py tests/integration/test_negative_available_balance_repro.py -q

64 tests passed. Ruff checks passed on changed files. Full-project mypy was attempted but did not finish within the tool timeout; no successful typecheck is claimed.

Final built image: localhost/routstr-reserved-repro:fix, ef81426ad79e3d14ec462a39ab1f7481fd0cb410a9cb93cb42de41e8b3523869.

Container tests used real TCP, full middleware stack, frozen image dependencies, isolated SQLite and synthetic balances. Read timeout 3s, lifetime 15s, delivery timeout 2s, cleanup timeout 3s, stale timeout 6s.

Reused the main probe on ports 18100/18101. Results in results-final.txt and router-final.log:

- Finite and silent streams settled.
- Header wait released its reservation.
- Disconnected endless stream no longer retained its reservation.
- Non-reading flood client hit bounded delivery/cleanup.
- Connected keepalive-only stream terminated at maximum lifetime.
- After the background-sweep interval and all client closures: every key reserved_balance=0, no active durable reservations. Explicit database assertions passed.
- Router shut down within the 10-second grace without SIGKILL. Dummy upstream still required SIGKILL: its fixture deliberately sleeps/open-streams and is not patched router code.

Actual mint payout was not tested. Protocol errors on already-started streams when deadlines interrupt them are expected; an HTTP status cannot be replaced after headers are sent.

## Financial policy / limitations

The lifecycle first lets existing finalization run within a bounded budget. If still active, fallback releases only that reservation; late charge is fenced by terminal state. This can forgo charging observed output on failed settlement. It prioritizes freeing customer funds over leaving them locked; review this policy before deployment. Upstream compute may continue remotely even after local connection closure.

This implementation does not complete every proposed hardening idea: provider cancellation APIs, full observability, per-record unexpected DB-failure isolation, legacy NULL aggregate background reconciliation, multi-worker/alternate-route network matrix and DB-outage injection remain follow-up work. No dependency upgrade was needed for the tested cases because explicit disconnect supervision avoids relying solely on send errors.

All reproduction containers are stopped. Original node data/configuration is untouched. Source changes are uncommitted in the worktree for review.
