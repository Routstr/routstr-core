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
| `routstr/confidential.py` | the Nostr-signed offer (`GET /v1/confidential/offer`), model selection (the candidate whose provider host is the sidecar's upstream), the signed rate snapshot (same rate function as billing), the `/v1/models` field (exactly the models in the signed `price_list`), and thin billing glue over the node's normal path (`reserve_deposit`, `reserve`, `reserve_image`, `settle`, `settle_flat`) |
| `routstr/confidential_ws.py` | `GET /v1/confidential/ws` (control + TLS relay to the sidecar) and `GET /v1/confidential/zk` (the 2PC channel, the proof channel, one proof) |
| `core/main.py`, `core/settings.py`, `payment/models.py`, `nostr/listing.py` | route registration, the settings, the additive models field, the listing's `confidential` tag |

The **cu-sidecar** (Rust, separate process on loopback; source, build and the
protocol: <https://github.com/jooray/routstr-confidential>, `crypto/relay`)
holds the provider key, verifies the relayed handshake, runs the node side of
the two-party key schedule and of the proofs, holds a request's records until
π_WIN verifies, and writes the credential record.

**Protocol version.** The node speaks only protocol v3 (`PROTOCOL-V3.md` in
that repository; its §12 is the wire format, with the node's messages marked
**N**). The offer says `v: 3`; `/v1/confidential/ws` answers a socket of any
other version (`v=2`, none) with
`{"type":"error","status":426,"reason":"upgrade_required","min_v":3,…}` and
closes, and so does a `setup` without `"v":3`; `/v1/confidential/zk` accepts
only `twopc`, `zkchan`, `pi_nk`, `pi_win` and `pi_c3s`; a sidecar whose offer
does not name `v: 3` is refused (no offer, no `confidential_upstream` models
field). Clients fetch the offer as `/v1/confidential/offer?v=3` (the query is
ignored), and the node's Nostr listing carries the tag `["confidential","3"]`
while a sidecar is configured. Protocol v2 remains on the branch
`confidential-2pc`, v1 on `confidential-upstream`.

## Connections and requests

The unit is a **connection**: one two-party TLS handshake with the provider.
It carries **requests**. The signed offer's `http` lists the framings of the
inner connection the sidecar serves, in its operator's order of preference:

| Framing | Inner connection | Requests |
|---|---|---|
| `"2"` | HTTP/2 | up to `h2.max_requests`, one after another |
| `"1.1"` | HTTP/1.1 | exactly one, sent before the ClientHello |

Both use the same messages:

1. `setup {v: 3, http, nonce_c, armed, auth, offer_sig}` names the connection
   only: no model, no `max_tokens`, no fees (a setup that names one is
   refused). The node checks, in this order and before it dials the sidecar:
   the shape; that the sidecar serves `http` now (400); that `offer_sig`
   names an offer it signed and remembers (409 `offer_expired`); that the
   credential is valid with an available balance of at least
   `max(1 msat, MIN_REQUEST_MSAT, session fee, abandon fee)` (401/402; a
   Cashu token is redeemed here); and it reserves the **connection deposit**
   (402 `connection_deposit_unfunded`). Then it takes a session slot, dials
   the sidecar and forwards `{type, v, http, nonce_c, armed, escrow}`.
   `ready` comes back with the node's `pubkey`, `zk_token`, `sig_scheme` and
   `deposit_msat` (what was reserved). An armed connection is bound to the
   credential only.
2. `request {n, kind, model, max_tokens | variant + count, body_len, auth,
   offer_sig, session_fee_msat, abandon_fee_msat}` is what the node bills:
   it validates the terms, freezes the rates of the offer the request names,
   reserves, and forwards the request to the sidecar without the credential,
   the offer signature and the fees (an image request with the node's own
   rendering of the suffix). `auth` and `offer_sig` may differ from the
   setup's.
3. A request the node or the sidecar does not accept is answered with
   `request_refused {n, status, reason[, detail]}`: nothing is held for it,
   the connection stays, and the next `request` carries the same `n`. A wrong
   `n`, a second request while one is in flight, a second accepted request on
   framing `"1.1"`, or a framing-`"2"` request before `head_written` is a
   protocol error instead: `error`, and the connection ends.
4. The node relays everything else. It reads `body_released` (the provider
   has the complete request), re-signs the `attestation`, and settles on the
   sidecar's `usage` frame with a signed `receipt`.
5. The connection's last frame is `closed {reason}` or `error`. When the
   deposit was not released by a settlement, that frame carries its outcome.

**The offer a connection was opened on.** The node remembers a signed offer
for 10 minutes after it last served it, and for as long as a connection
opened on it is alive. A request may name a newer offer (its rates and fees
are then the newer ones) as long as that offer's connection-level fields
(`v`, `twopc`, `upstream_host`, `head_template`, `chat`, `h2`,
`connection_deposit_msat`, `idle_timeout_s`, `images` without its prices) are
the same and it still lists the connection's framing; otherwise the request
is refused with 409 `offer_changed` and the client opens a new connection or
names the old offer.

## Billing (the node's normal path)

Requests are billed like any request whose prompt the node cannot read (cf.
EHBP), at rates fixed when the request is accepted:

1. The credential is the client's ordinary bearer: an `sk-` key or a Cashu
   token. `validate_bearer_key` turns it into an account.
2. The request's rates (msats per 1k tokens: `in`, `cached_in`,
   `cache_write`, `out`) are the `price_list` entry of the offer its
   `offer_sig` names. An unknown or expired offer, or a model it does not
   price, is refused (409 `offer_expired`) and the client refetches the offer.
   At most 512 offers without a live connection are kept.
3. The prompt is opaque to the node, so `pay_for_request` reserves the
   full-context prompt plus the pinned `max_tokens` at the frozen rates, plus
   the larger fee:
   `max(MIN_REQUEST_MSAT, ceil((prompt_limit*max(in,cache_write) + (max_tokens+margin)*out)/1000)) + max(session fee, abandon fee)`,
   where `prompt_limit` is the model's full context length (a prompt cap
   smaller than that would leave part of an accepted prompt unreserved) and
   `margin` is the offer's `reserve_margin_tokens` (providers may bill a few
   completion tokens past the cap; the charge is still the usage). Usage
   beyond the reservation is paid by the node, so size the margin for the
   provider. A model without a known context length reserves its undiscounted
   maximum cost plus the margin; a model whose metadata lists a negative
   context length is refused. An image request reserves
   `msat_per_image × count` plus the larger fee (`reserve_image`).
4. On the π_C3S-verified usage the node charges exactly
   `ceil((in*(prompt-cached-written) + min(cached_in,in)*cached + cache_write*written + out*completion)/1000)`,
   capped at the reservation, where `cached` is
   `usage.prompt_tokens_details.cached_tokens` (at most `prompt`) and
   `written` the prompt-cache writes (at most `prompt-cached`): the first
   positive integer of `prompt_tokens_details.cache_creation_input_tokens`,
   `…cache_write_tokens`, `…cache_creation_tokens` and the top-level
   `cache_creation_input_tokens`. An offer signed without `cache_write` bills
   writes at `in`. The reservation is finalized through the EHBP actual-cost
   path (released when the cost is zero). A usage event without token counts
   charges the reservation; one that names another spelling of the pinned
   model is settled at the pinned rates and logged. An image request is
   charged `msat_per_image × count` when the client accepts the response
   (`settle_flat`).
5. An upstream HTTP error is disclosed from the response's status line and
   headers only (framing `"2"`: π_WIN under `s_ap_0`; `"1.1"`: π_C3S); the
   node learns the status and those headers, never the error body.
6. Unused balance is withdrawn with the existing `/v1/wallet/refund`.

### What a request costs

"Still due" means: no request of this connection was settled yet.

| The request ends… | Charge |
|---|---|
| refused (`request_refused`) | nothing; never reserved, or released in full when the sidecar refused it |
| settled 2xx | chat: the usage at the frozen rates; images: `msat_per_image × count`; plus the session fee if still due |
| error head 429/503 (the provider's capacity) or 401/402/403 (the node's own provider key or account) | the session fee if still due |
| error head, any other 4xx (a request the provider rejected) | the abandon fee |
| error head, any other 5xx | the reservation (the provider may have billed work) |
| accepted, and the connection ends before `body_released` (a failed π_WIN, the client leaves, the time limit) | the abandon fee; nothing when the deposit is charged for it (below), and nothing when the sidecar named the provider or itself as the cause (`fault`) |
| before `body_released`, the provider answered and closed (`upstream_closed_early` with response bytes) | the session fee if still due |
| after `body_released` with no settlement | the reservation. (EHBP can charge nothing here because the node reads the usage itself; in this mode the client controls the disclosure.) |

A usage disclosed before the sidecar's attestation, or a settlement the node
cannot write after three retries, is the node's own failure: the client gets
`error` with `fault: "node"` instead of a receipt. Each reservation is
finalized exactly once, before the connection's last frame is sent.

### Fees, deposit and surcharge

A connection costs the node far more than a plain request: about 16 MB of 2PC
data and ~0.5 s of CPU, before the provider sees anything, and a pre-warmed
connection does all of it before a request exists. Four settings price that
work.

- `CONFIDENTIAL_RATE_SURCHARGE_PCT` raises the per-token rates and the image
  prices. The signed offer carries the surcharged prices (the ones billed), so
  the client's rate check is unchanged; the offer also states
  `rate_surcharge_pct`.
- `CONFIDENTIAL_SESSION_FEE_MSAT` (`session_fee_msat`) pays for the handshake.
  It falls due **once per connection**, with the first settlement of a request
  the provider received. A request's receipt names it only when that request
  was charged it.
- `CONFIDENTIAL_ABANDON_FEE_MSAT` (`abandon_fee_msat`), never below the
  session fee (unset, it equals it): per request, as in the table above.
- `CONFIDENTIAL_CONNECTION_DEPOSIT_MSAT` (`connection_deposit_msat`): what a
  `setup` reserves, for both framings, through the node's ordinary
  reservation. Default: the largest of the abandon fee, the session fee and
  1000 msat; `0` takes none.

A client accepts the fees by echoing `session_fee_msat` and
`abandon_fee_msat` of the offer in every `request`; one that does not is
refused (409 `session_fee_not_accepted`). The deposit is accepted by naming
the offer in the `setup`. The node bills at the terms of the offer named by
`offer_sig`, even if the settings changed since.

**The deposit** is released when the connection's first request reaches a
settlement (usage, `accept`, or an error head); that receipt carries
`deposit_released_msat`. A connection that ends with no settled request,
first matching row:

| # | How the connection ended | Deposit | `reason` |
|---|---|---|---|
| 1 | the setup was refused before the sidecar was dialed | never reserved | (the `error`) |
| 2 | no session slot, the sidecar unreachable, or it refused the setup | released | `not_started` |
| 3 | the sidecar reported `fault: "upstream"` (the provider unreachable, `h2` not selected, framing `"2"` withdrawn) | released | `provider_unreachable` |
| 4 | the sidecar reported `fault: "node"`, or its socket ended without `error` or `closed` | released | `sidecar_failure` |
| 5 | the provider closed the open, idle connection before any request | released | `provider_closed` |
| 6 | the node refused a request of this connection with a 5xx, or a settlement's billing write failed | released | `node_failure` |
| 7 | a request was written in full (`body_released`) and never settled | released (the reservation is charged) | `request_received` |
| 8 | the provider answered a request and closed before its body was complete | released (the request keeps the session fee) | `provider_refused` |
| 9 | a request was accepted and the connection ended before its `body_released` for any other reason | **charged**; the reservation is released in full | `request_abandoned` |
| 10 | no request in flight for `idle_timeout_s` | **charged** | `idle` |
| 11 | `head_written` not reached within 10 minutes | **charged** | `setup_timeout` |
| 12 | anything else: the client left, a failed handshake or π_NK, a protocol error, only refused requests | **charged** | `unused` |

The connection's last frame then carries
`deposit {sid, msat, outcome, reason, cost_msats, balance_msats, time}` with
`deposit_json` (the signed bytes), `deposit_sig` and `sig_scheme`. A client
that leaves is never read as a sidecar failure: row 4 needs the sidecar's
socket to end while the client's is still there. If the node process dies,
the reservation's lease lapses and the stale-reservation sweeper releases it.

Example: session fee 3 sat, abandon fee 5 sat, deposit 7 sat. A connection
with three chat requests costs their usage + 3 sat once; 7 sat are held from
the setup to the first receipt. A request the provider rejects with 400 costs
5 sat. A connection opened and dropped costs 7 sat.

### Receipt, attestation

The receipt (signed with the node's Nostr key, `sig_scheme:
"schnorr-secp256k1"`, sent as `{type: "receipt", receipt, receipt_json, sig,
sig_scheme}`; `receipt_json` is the signed text):

| Field | Meaning |
|---|---|
| `v` | 1 (the receipt format) |
| `sid`, `http`, `n` | the connection, its framing (`"2"` or `"1.1"`), the request |
| `model` | the pinned model |
| `usage` | what was settled: the provider's usage event, `{"error_status", "choices": []}`, or `{"flat": true, "kind": "images", "count"}` |
| `cost_msats`, `reserved_msats`, `balance_msats` | the charge, what was reserved for the request, the key's balance after it |
| `rates` | chat: the frozen rates |
| `kind: "images"`, `variant`, `count`, `msat_per_image` | image requests (instead of `rates`) |
| `session_fee_msat` | only when this request owed it (the connection's first settlement) |
| `abandon_fee_msat`, `rate_surcharge_pct` | the offer's, when set |
| `deposit_released_msat` | only on the connection's first settled request, when a deposit was held |
| `notary_pubkey` | the node's key |
| `attestation_hash` | SHA-256 of the attestation's `a_json` |
| `time` | Unix seconds |

The attestation is the sidecar's (`a`: the sid, the framing, the request,
record counts and SHA-256 of the bytes each way, the certificate-chain hash,
both handshake transcript hashes, `c_commit`, the time; per request on
framing `"2"`, at the provider's close on `"1.1"`). The node passes it on as
`{type: "attestation", n, a, a_json, sig, sig_scheme, sidecar_sig}`: `a_json`
is the canonical JSON it signed with its Nostr key (`sig`), `sidecar_sig` the
sidecar's Ed25519 signature. An attestation that names another sid is not
signed.

## Limits

- The node dials the sidecar only after a valid `setup` whose deposit is
  reserved. The `setup` must arrive within 10 s of the websocket opening.
- Until then a connection is pending. At most 1024 connections are pending
  per node process and 4 per client address (an IPv6 address counts by its
  /64). A new connection that finds either full closes the oldest one still
  waiting for its `setup` (503) rather than being refused; it is refused
  (429/503) only when every pending connection there is already being
  reserved.
- The sidecar does not check client credentials, so it must be reachable
  only by the node: every node request to it carries
  `CONFIDENTIAL_SIDECAR_TOKEN`, and a sidecar off loopback refuses requests
  without it (and refuses to start without a token).
- At most 32 connections at once per node process, each for its whole life.
  A setup that finds all taken is refused (503) and its deposit released.
- A connection counts as unused for `CONFIDENTIAL_MAX_UNUSED_PER_IP` until
  the first `body_released` of one of its requests, or until it ends.
- Time: 10 minutes from the websocket accept to `head_written` (the armed
  wait included); per request 10 minutes from the `request` to
  `body_released` and 10 more to the settlement (within the reservation's
  lease less the 60 s settlement grace); with no request in flight the node
  closes the connection after `idle_timeout_s`
  (`CONFIDENTIAL_IDLE_TIMEOUT_S`, at least 10 s and never more than the
  sidecar's `h2.idle_secs`), counted from `head_written` or the last
  settlement, with `{"type":"closed","reason":"idle"}`. After its first
  settlement an idle connection holds no reservation.
- At a time limit the node closes the sidecar's session and hears its last
  frames (5 s) before the money decision, so a `body_released` on its way is
  honoured.
- `request.max_tokens` is an integer in `[1, max_tokens_cap]`, `body_len` in
  `[1, 8 MiB]`, `count` in `[1, 4]` and at most the offer's and the variant's
  `max_count`. A `setup` or `request` frame larger than 256 KiB is refused
  unparsed.
- Every refusal of a setup or failure is sent as
  `{"type":"error","status":<int>,"reason":<str>,"detail":<…>}` before the
  socket closes (400 bad setup, 402 balance, 408 timeout, 409 stale offer,
  413 frame too large, 426 another protocol version, 429/503 limits,
  502 sidecar unreachable, 500 internal). The node's own failures carry
  `fault: "node"`.
- The sidecar's offer is fetched with a 3 s timeout and cached for 30 s; a
  failed fetch is remembered for 10 s, and the last good offer is served for
  up to 10 minutes while the sidecar cannot be reached, so `/v1/models` never
  waits on it for long. So the node's `http` follows a sidecar that withdrew
  framing `"2"` within 30 s; a `setup` for `"2"` in between is refused by the
  sidecar and its deposit released (row 3). A sidecar that answers with an
  offer the node refuses (malformed, not run with `--node-settles`, another
  protocol version) is not served from that cache.
- The offer's `ws` URL is built from `HTTP_URL` (`https`→`wss`); without it the
  offer carries the relative path `/v1/confidential/ws`.
- `/v1/confidential/zk` admits only the sid of a connection that is live on
  the same node process, with the `zk_token` of its `ready`: at most 6 proof
  connections per connection and 192 per process at once. A `/zk` proxy
  closes with its connection.
- A reservation's lease (its expiry and heartbeat) covers both 10-minute
  phases of its request plus 60 s for settlement, the deposit's also the
  setup phase and one idle period, even when `MAX_REQUEST_LIFETIME_SECONDS`
  is shorter, so the stale-reservation sweeper never releases one mid-way.
- Not offered under `FIXED_PRICING`: a flat per-request price cannot cover an
  opaque request of up to the model's full context.
- The signed offers, the live connections and the per-address counts are kept
  per process: run the node as a single worker process (one uvicorn worker,
  with `--ws-ping-timeout 300`). The node refuses to start when its launcher
  names more than one worker, or when another process of the same node holds
  its lock file (`routstr-confidential-<uid>/` in `$XDG_RUNTIME_DIR` or the
  temporary directory; the directory must be this user's, mode 0700).

## Configuration

| Env | Meaning | Default |
|---|---|---|
| `CONFIDENTIAL_SIDECAR_URL` | cu-sidecar base URL; empty disables the mode; env-only, read at startup (the routes are mounted then), never stored in the database | `""` |
| `CONFIDENTIAL_SIDECAR_TOKEN` | shared secret sent to the sidecar as `Authorization: Bearer` (the sidecar's `CU_SIDECAR_TOKEN`); required when the sidecar is not on loopback, since it does not check client credentials itself; env-only, never stored in the database | `""` |
| `CONFIDENTIAL_CONNECTION_DEPOSIT_MSAT` | deposit (msat) a `setup` reserves, released by the connection's first settlement, charged when it ends without one unless the node, the sidecar or the provider failed; `-1` = the largest of the abandon fee, the session fee and 1000 msat; `0` = none; a value above 0 is at least `MIN_REQUEST_MSAT`; in the signed offer as `connection_deposit_msat` | `-1` |
| `CONFIDENTIAL_IDLE_TIMEOUT_S` | seconds without a request in flight after which the node closes a connection (at least 10, never more than the sidecar's `h2.idle_secs` while it serves framing `"2"`); in the signed offer as `idle_timeout_s` | `120` |
| `CONFIDENTIAL_SESSION_FEE_MSAT` | fee (msat) due once per connection, with its first settlement: on top of a settled 2xx, alone for a provider refusal (429/503/401/402/403); in the signed offer; clients echo it in each request | `0` (off) |
| `CONFIDENTIAL_ABANDON_FEE_MSAT` | fee (msat) for a request the provider rejected (another 4xx) and for an accepted request dropped before `body_released` once the deposit was released; never below the session fee (0 = equal to it) | `0` (off) |
| `CONFIDENTIAL_RATE_SURCHARGE_PCT` | percentage added to the per-token rates and image prices; the signed offer carries the surcharged prices | `0` (off) |
| `CONFIDENTIAL_RESERVE_MARGIN_TOKENS` | completion tokens reserved beyond the pinned `max_tokens` (a provider may bill a few more than the cap, and the charge never exceeds the reservation); only the hold grows; a usage with more completion tokens than `max_tokens` plus this is logged as a warning; in the signed offer as `reserve_margin_tokens` | `16` |
| `CONFIDENTIAL_MAX_UNUSED_PER_IP` | refuse (429) a new connection from a client address (IPv6: its /64) that already has this many unused ones (setup reserved, no request released yet; armed connections count); 0 disables | `12` |
| `CONFIDENTIAL_TRUSTED_PROXIES` | comma-separated peers whose client-address header is believed (e.g. `127.0.0.1,::1` for nginx on the same host); `self` adds the node's own addresses (enable only behind a reverse proxy that always sets the header) | empty (no peer is trusted) |
| `CONFIDENTIAL_CLIENT_IP_HEADER` | header a trusted proxy puts the client address in; for `X-Forwarded-For` the rightmost entry over all its header lines is used; another header is read from its last line | `x-forwarded-for` |
| `NSEC` | node key; signs offer, attestation, receipt and deposit outcome (required) | |

Accepted mints, pricing and refunds are the node's existing settings. Apart
from the env-only `CONFIDENTIAL_SIDECAR_URL` and `CONFIDENTIAL_SIDECAR_TOKEN`,
these are persisted in its database on first start, like the node's other
settings: afterwards a value in the database wins over the environment, so
change them with the admin settings API (`PATCH /admin/api/settings`, e.g.
`{"confidential_session_fee_msat": 2000, "confidential_abandon_fee_msat": 10000}`)
or the dashboard. Clients pick up a change with the next offer; a connection
keeps the deposit and idle timeout of the offer it was opened on, and a
request the fees and rates of the offer it names.

Upgrading from protocol v2: `CONFIDENTIAL_H2_CONNECTION_DEPOSIT_MSAT` and
`CONFIDENTIAL_H2_IDLE_TIMEOUT_S` of the prototype are now
`CONFIDENTIAL_CONNECTION_DEPOSIT_MSAT` and `CONFIDENTIAL_IDLE_TIMEOUT_S`; the
sidecar must be the v3 one (its offer names `v: 3` and `twopc`); v2 clients
get the 426 above until they are updated.

## Tests

```bash
.venv/bin/python -m pytest tests/unit/test_confidential.py tests/unit/test_confidential_v3.py -q
```

Live, against a real provider (routstr-confidential repository):
`crypto/tools/v3-node-check.sh` starts a sidecar and this node on scratch
state and checks the money outcome of every case in the node's database.

## Client addresses

The per-address limit identifies clients by the TCP peer address, or, when the peer is a trusted
proxy, by the proxy's header. Without a reverse proxy nothing needs configuring, as long as the node
sees real peer addresses. A container port forwarder that rewrites them (rootless podman/docker
publishing a port) makes every client look like the node itself: put a reverse proxy in front and set
`CONFIDENTIAL_TRUSTED_PROXIES=127.0.0.1,::1,self`, or raise or disable the limit. Never trust `self`
without such a proxy, since any client could then name its own address. Tor clients all arrive from the
Tor daemon and share one limit.

No peer is trusted by default. A reverse proxy on the same host (nginx connecting over loopback) needs
`CONFIDENTIAL_TRUSTED_PROXIES=127.0.0.1,::1`; without it all its clients share the proxy's address and one
limit. Trust an address only if every connection from it comes through a proxy that sets the header: a
Tor daemon or any other local process that connects to the node's loopback port directly could otherwise
name any client address. A node whose database already holds a value keeps it (the database wins over
the environment); a new node starts with none.
