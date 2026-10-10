# Confidential-upstream mode (node side)

The client (routstrd / `@routstr/sdk`) becomes the TLS 1.3 client of the
upstream provider through this node. The node relays TLS records and writes
the one record that carries its provider API key. It never sees the prompt or
the response; the client never sees the API key. Protocol, security analysis
and measurements: the *Confidential and verifiable upstream inference over
standard TLS 1.3* paper.

Everything here is **additive and off by default**: with
`CONFIDENTIAL_SIDECAR_URL` unset none of the routes below exist and
`/v1/models` is unchanged.

## How it plugs in

| Piece | What |
|---|---|
| `routstr/confidential.py` | the Nostr-signed offer (`GET /v1/confidential/offer`), model selection (the candidate whose provider host is the sidecar's upstream), the signed rate snapshot (same rate function as billing), the `/v1/models` field (exactly the models in the signed `price_list`), and thin billing glue over the node's normal path |
| `routstr/confidential_ws.py` | `GET /v1/confidential/ws` (control + TLS relay to the sidecar) and `GET /v1/confidential/zk` (one ZK proof) |
| `core/main.py`, `core/settings.py`, `payment/models.py` | route registration, the one setting, the additive models field |

The **cu-sidecar** (Rust, separate process on loopback; source, build and the
protocol paper: <https://github.com/jooray/routstr-confidential>, `crypto/relay`)
holds the provider key, verifies the relayed handshake, runs the node side of the proofs, holds
the record that completes the request body until π_C2 verifies, and writes the
credential record.

## Billing (the node's normal path)

Confidential sessions are billed like any request whose prompt the node
cannot read (cf. EHBP), at rates fixed when the session starts:

1. The client's `setup` frame carries its ordinary bearer: an `sk-` key or a
   Cashu token. `validate_bearer_key` turns it into an account. The setup may
   also carry `offer_sig`, the `sig` of the signed offer the client verified.
2. The session's rates (msats per 1k tokens: `in`, `cached_in`, `out`) are
   frozen at setup. With `offer_sig` they are that offer's `price_list` entry
   for the model; the node honours offers it signed in the last 10 minutes
   (an unchanged offer keeps its signature). An unknown or expired
   `offer_sig`, or a model the offer does not price, is refused with
   `409` / `offer_expired`, and the client refetches the offer. Without
   `offer_sig` the current rates are frozen.
3. The prompt is opaque to the node, so `pay_for_request` reserves the
   full-context prompt plus the pinned `max_tokens` at the frozen rates:
   `max(MIN_REQUEST_MSAT, ceil((prompt_limit*in + max_tokens*out)/1000))`,
   where `prompt_limit` is the model's context length minus its maximum
   completion tokens (the same context logic as normal requests). A model
   without a known context length reserves its undiscounted maximum cost;
   fixed-pricing deployments reserve their fixed per-request cost. The bearer
   and `offer_sig` are not forwarded to the sidecar; the reservation id binds
   the session id instead.
4. On the π_C3-verified usage the node charges exactly
   `ceil((in*(prompt-cached) + min(cached_in,in)*cached + out*completion)/1000)`,
   capped at the reservation, where `cached` is
   `usage.prompt_tokens_details.cached_tokens`. This never exceeds
   `ceil((in*prompt + out*completion)/1000)` at the signed rates, which is what
   the client checks. The reservation is finalized through the EHBP
   actual-cost path (released when the cost is zero). A usage event for
   another model, or without token counts, charges the reservation.
5. The node then sends a signed receipt with the usage, the cost, the
   reservation, the frozen rates and the new balance.
6. An upstream HTTP error is disclosed under π_C3 from the response's status
   line and headers only; the node learns the status and those headers, never
   the error body. A 4xx or 503 releases the reservation (less the session fee,
   if set; below); other 5xx charge it.
7. An abort before the sidecar released the final body record releases the
   reservation (less the abandon fee, if set; below), because the upstream
   never had a complete request. After the
   release, a missing usage disclosure charges the reservation. (EHBP can
   charge nothing in that case because the node reads the usage itself; here
   the client controls the disclosure.) If settlement fails, the same rule
   applies and the client gets an error frame. Each reservation is finalized
   exactly once.
8. Unused balance is withdrawn with the existing `/v1/wallet/refund`.

### Session fees and rate surcharge (optional, off by default)

A confidential session costs the node far more than a plain request: about
22 MB of 2PC data and ~0.5 s of CPU per session, before the provider sees
anything, and a pre-warmed session does all of it before a request exists.
Three settings price that work; all are 0 by default, and then the offer, the
setup, the receipt and every charge are exactly as above.

- `CONFIDENTIAL_RATE_SURCHARGE_PCT` raises the per-token rates of confidential
  sessions by that percentage. The signed offer's `price_list` carries the
  surcharged rates (the ones billed), so the client's rate check is
  unchanged; the offer also states `rate_surcharge_pct`.
- `CONFIDENTIAL_SESSION_FEE_MSAT` (`session_fee_msat`): a flat fee on top of
  the usage of a **completed** request.
- `CONFIDENTIAL_ABANDON_FEE_MSAT` (`abandon_fee_msat`): the anti-DoS deposit,
  charged instead when a session ends **without** a request reaching the
  provider. It is never below the session fee; unset (0), it equals the
  session fee. The node cannot tell a pre-warmed session from a cold one and
  does not need to: any session that ends that way pays it.

Both fees go into the signed offer (and, like the surcharge, into the model
listing's `confidential_upstream` field, so clients can size their deposit):

| Session outcome | Charge |
|---|---|
| completed (usage disclosed and verified) | usage at the (surcharged) rates + session fee, capped at the reservation |
| provider refused (4xx or 503, disclosed under π_C3) | session fee: the request reached the provider and the node did the 2PC, but no inference ran |
| ended before the final body record was released, after the sidecar accepted the setup (`ready`): armed and never used, aborted, failed proof, client gone | abandon fee; the rest of the reservation is released |
| the sidecar never accepted the setup (unreachable, refused) | nothing |
| other 5xx, or no disclosure after the body was released | the reservation, as before |

- **Acceptance.** A client accepts the fees by echoing `session_fee_msat` and
  `abandon_fee_msat` in its `setup`. A setup that does not echo exactly the
  frozen offer's fees is refused before anything is reserved (`409` /
  `session_fee_not_accepted`), so a client that does not know about fees is
  never charged one silently. The node bills at the terms of the offer named
  by `offer_sig` (fees and rates), even if the settings changed since.
- **Reservation** = the usual reservation (at the surcharged rates) +
  max(session fee, abandon fee).
- **Receipts** carry `session_fee_msat`, `abandon_fee_msat` and
  `rate_surcharge_pct` (only when set). Clients check
  `cost ≤ ceil((in·prompt + out·completion)/1000) + session fee` at the signed
  rates, and `cost ≤ session fee` for a 4xx/503. An abandoned session gets no
  receipt; its charge (the abandon fee) shows in the balance.

Example: abandon fee 10 sat, session fee 2 sat. A pre-warmed session that is
used costs its usage + 2 sat (the other 8 sat of the deposit are released with
the reservation); one that is never used costs 10 sat.

The fees combine with the per-address unused-session limit
(`CONFIDENTIAL_MAX_UNUSED_PER_IP`): the limit caps how many sessions one
address can hold unused at a time, the abandon fee makes each one cost money.
Without fees, an armed and abandoned session is free for the client. Clients
that pre-warm (routstrd, the SDK) arm at most one session for a node that
charges an abandon fee, and none when it exceeds their `maxSessionFeeMsat`.

## Session limits

- The node dials the sidecar only after a valid `setup` has been reserved.
  The `setup` must arrive within 15 s of the websocket opening.
- `setup` is validated before anything is reserved: `model` a non-empty
  string, `max_tokens` an integer in `[1, max_tokens_cap]`, `len` (the request
  body size) an integer in `[1, 8 MiB]`, `nonce_c` 64 hex characters.
- At most 32 confidential sessions run at once per node process; a session
  lasts at most 10 minutes.
- Every refusal or failure is sent as
  `{"type":"error","status":<int>,"reason":<str>,"detail":<…>}` before the
  socket closes (400 bad setup, 402 balance, 408 timeout, 409 stale offer,
  502/503 sidecar unavailable or session limit, 500 internal).
- The sidecar's offer is fetched with a 3 s timeout and cached for 30 s; a
  failed fetch is remembered for 10 s, and the last good offer is served for
  up to 10 minutes while the sidecar cannot be reached, so `/v1/models` never
  waits on it for long.
- The offer's `ws` URL is built from `HTTP_URL` (`https`→`wss`); without it the
  offer carries the relative path `/v1/confidential/ws`.

## Configuration

| Env | Meaning | Default |
|---|---|---|
| `CONFIDENTIAL_SIDECAR_URL` | cu-sidecar base URL; empty disables the mode | `""` |
| `CONFIDENTIAL_MAX_UNUSED_PER_IP` | refuse (429) a new session from a client address that already has this many unused sessions (set up, credential record not yet written; pre-warmed sessions count); 0 disables | `3` |
| `CONFIDENTIAL_TRUSTED_PROXIES` | comma-separated peers whose client-address header is believed; `self` adds the node's own addresses (where a rootless container port forwarder connects from; enable only behind a reverse proxy that always sets the header) | `127.0.0.1,::1` |
| `CONFIDENTIAL_SESSION_FEE_MSAT` | fee (msat) on top of the usage of a completed confidential request, and the charge for a provider refusal (4xx/503); published in the signed offer; clients must accept it at setup | `0` (off) |
| `CONFIDENTIAL_ABANDON_FEE_MSAT` | fee (msat) for a session the sidecar accepted that ends without a request reaching the provider (armed and never used, aborted, client gone): the anti-DoS deposit, reserved at setup; never below the session fee (0 = equal to it) | `0` (off) |
| `CONFIDENTIAL_RATE_SURCHARGE_PCT` | percentage added to the per-token rates of confidential sessions; the signed offer's `price_list` carries the surcharged rates | `0` (off) |
| `CONFIDENTIAL_CLIENT_IP_HEADER` | header a trusted proxy puts the client address in; for `X-Forwarded-For` the rightmost entry is used | `x-forwarded-for` |
| `NSEC` | node key; signs offer, attestation and receipt (required) | |

Accepted mints, pricing and refunds are the node's existing settings. Like
the node's other settings, these are persisted in its database on first start:
afterwards a value in the database wins over the environment, so change them
with the admin settings API (`PATCH /admin/api/settings`, e.g.
`{"confidential_session_fee_msat": 2000, "confidential_abandon_fee_msat": 10000}`)
or the dashboard. Clients pick up a
change with the next offer; sessions set up on an older signed offer (up to
10 minutes) keep its terms.

## Tests

```bash
.venv/bin/python -m pytest tests/unit/test_confidential.py -q
```

## Client addresses

The per-address limit identifies clients by the TCP peer address, or, when the peer is a trusted
proxy, by the proxy's header. Without a reverse proxy nothing needs configuring, as long as the node
sees real peer addresses. A container port forwarder that rewrites them (rootless podman/docker
publishing a port) makes every client look like the node itself: put a reverse proxy in front and set
`CONFIDENTIAL_TRUSTED_PROXIES=127.0.0.1,::1,self`, or raise or disable the limit. Never trust `self`
without such a proxy, since any client could then name its own address. Tor clients all arrive from the
Tor daemon and share one limit.
