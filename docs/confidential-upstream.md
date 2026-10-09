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
   the error body. A 4xx or 503 releases the reservation; other 5xx charge it.
7. An abort before the sidecar released the final body record releases the
   reservation, because the upstream never had a complete request. After the
   release, a missing usage disclosure charges the reservation. (EHBP can
   charge nothing in that case because the node reads the usage itself; here
   the client controls the disclosure.) If settlement fails, the same rule
   applies and the client gets an error frame. Each reservation is finalized
   exactly once.
8. Unused balance is withdrawn with the existing `/v1/wallet/refund`.

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
| `NSEC` | node key; signs offer, attestation and receipt (required) | |

Accepted mints, pricing and refunds are the node's existing settings.

## Tests

```bash
.venv/bin/python -m pytest tests/unit/test_confidential.py -q
```
