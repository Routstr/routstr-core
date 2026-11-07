# EHBP Proxy Support for Tinfoil Models

## Problem

The SDK's `SecureClient.fetch` encrypts request bodies with HPKE (EHBP protocol)
and sends them to the Routstr provider. The Routstr proxy had no EHBP handling:

1. It tried to `json.loads()` the binary HPKE-sealed body → failed with a 400
2. The upstream PPQ.AI public endpoint (`/v1/chat/completions`) doesn't speak
   EHBP, so the response had no `Ehbp-Response-Nonce` header
3. `SecureClient` threw `Missing Ehbp-Response-Nonce header` because it expects
   every response from an EHBP-configured `baseURL` to carry that header

## Root cause

PPQ.AI exposes EHBP-aware inference at `/private/`, separate from the public
`/v1/` endpoint. The Routstr proxy was forwarding to `/v1/` (the public
endpoint) instead of `/private/` (the enclave endpoint). The public endpoint
can't decrypt the body, returns a normal HTTP response, and the SDK can't
decrypt it because there's no nonce header.

The PPQ private-mode proxy (`ppq-private-mode-proxy/lib/proxy.ts`) shows the
correct pattern: `SecureClient` talks to `api.ppq.ai/private/v1/chat/completions`,
which decrypts inside the attested enclave and returns an EHBP-encrypted
response with the `Ehbp-Response-Nonce` header.

## What was changed

### `routstr/proxy.py`

Detects EHBP requests by checking for the `Ehbp-Encapsulated-Key` header (set
by the EHBP transport on every encrypted request). For EHBP requests:

- Skips JSON body parsing (the body is binary ciphertext, not JSON)
- Reads the model ID from the `X-Routstr-Model` header (set by the SDK) instead
  of from `body.model`
- Routes through new `forward_ehbp_request` (bearer auth) and
  `forward_ehbp_x_cashu_request` (x-cashu auth) methods
- Skips reactive 400 param correction (can't parse encrypted response body)
- Still charges the user via `pay_for_request` (uses `max_cost_for_model` from
  the model registry, not the body)

### `routstr/upstream/base.py`

Keeps EHBP as an explicit opt-in provider capability instead of making every
upstream provider appear EHBP-capable:

- `supports_ehbp = False` by default
- `get_ehbp_forwarding_target(path, model_obj)` raises `NotImplementedError`
  unless a provider opts in and returns a provider-specific EHBP target

The actual EHBP forwarding logic does **not** live in `base.py`.

### `routstr/upstream/ehbp.py`

Contains the shared opaque EHBP transport and billing helpers:

- `EHBPForwardingTarget` — provider-specific target URL plus extra headers
- `forward_ehbp_request()` — forwards the encrypted body, captures Tinfoil
  usage from a response header or streaming HTTP trailer, and finalizes bearer
  billing at actual cost (releasing the reservation when usage is unavailable)
- `forward_ehbp_x_cashu_request()` — redeems the Cashu token, refunds the full
  token on upstream failure, and refunds the difference between the redeemed
  amount and actual cost (or the full amount when usage is unavailable)

### Provider support

EHBP is currently enabled only for `TinfoilUpstreamProvider`. It forwards to
Tinfoil's attested enclave and requests `X-Tinfoil-Usage-Metrics` for billing.
PPQ.AI retains its private-target implementation, but `supports_ehbp = False`
until it has a provider-specific trusted usage/model-binding strategy.

## Why it's done this way

The proxy is a **blind relay** for EHBP requests. It cannot decrypt the body
(only the attested enclave can), so it must:

1. Get the model ID from a header, not the body
2. Forward the raw bytes without parsing or transformation
3. Stream the response back without SSE/cost parsing
4. Pass through EHBP protocol headers (`Ehbp-Encapsulated-Key` on request,
   `Ehbp-Response-Nonce` on response)

Cost tracking happens at the proxy level. Routstr reserves or redeems up to
`max_cost_for_model`, then Tinfoil's out-of-band usage header/trailer allows it
to finalize at actual token cost. If trusted usage is missing or invalid, the
proxy releases/refunds rather than treating the authorization ceiling as usage.

### Output-cap reservations — scaffolded, not active

EHBP hides the input and completion settings. The deployed router must enforce
visible headers before Core can safely quote a smaller hold:

- [PR518](https://github.com/tinfoilsh/confidential-model-router/pull/518)
  applies `X-Tinfoil-Max-Output-Tokens` **per choice**, not per request.
- [PR532](https://github.com/tinfoilsh/confidential-model-router/pull/532)
  (merged `d7a8870`) overrides the sealed body's `n` with
  `X-Tinfoil-Completion-Count` on **exact `/v1/chat/completions`**. Both headers
  are positive integers. It leaves `best_of` unchanged and guarantees a single
  model call, not a total bound across server-side loops.

**Neither reservation capability is enabled in production code.** Follow-up
research verified the count override live at `inference.tinfoil.sh`: hidden
`n=3`, visible count=1 and cap=8 yielded one choice / 8 output tokens; count=2
yielded 16. Hidden `best_of=3` also reported 8 with count=1, which establishes
no multiplier only for the backend and shape tested, not a universal guarantee.

The activation blocker is now **orchestration/request-total billing**, not
whether PR532 is deployed. Upstream `ab9edf...`, `main.go:917-953`, dispatches
hidden tool profiles into `toolruntime/tool_loop.go:139-218`, which repeatedly
invokes the model and aggregates usage, with a possible forced final call.
The visible pair bounds a single call, not this accumulated bill. Router 502
retries also exist, but there is no proof they double-bill; that remains an
unverified risk rather than an observed multiplier. Activation needs proof that
all accepted sealed request shapes cannot bill beyond the reservation (or an
upstream enforced request-total budget), not an SDK guard or operator toggle.

Headers already forward unchanged independently of quoting (unless nominated
hop-by-hop by `Connection`). A direct malicious EHBP caller can bypass all SDK
checks, so Core trusts only verified enclave enforcement, never hidden body
claims. The shipped default keeps the existing full envelope; that preserves
the status quo rather than proving the full envelope covers arbitrary fan-out.

#### Single-choice chat scaffold and SDK contract

`EHBP_SINGLE_CHOICE_OUTPUT_TOKEN_RESERVATION_ENABLED = False` is an independent
code gate. When tested with that gate enabled, Core requires:

- the exact chat endpoint (version-prefix normalization is supported, trailing
  slashes are not quoted);
- exactly one visible canonical positive integer cap, at most `2**31 - 1`;
- exactly one visible canonical completion count of **1**;
- both values surviving provider preparation, proxy-header stripping, and the
  real hop-by-hop sanitizer, including all raw `Connection` nominations.

Missing, invalid, repeated, comma-joined, stripped headers or count > 1 retain
the full envelope. Non-Tinfoil candidates never inherit the quote. Reconciliation
and per-candidate failover reservation behavior are unchanged.

Only after independently verified activation would `/v1/info` advertise:

```json
"features": {
  "ehbp_single_choice_output_token_reservation": true,
  "ehbp_output_token_reservation_routes": ["chat/completions"]
}
```

An SDK must require **both** the new feature value `=== true` and route membership,
then send both cap and count=1. It must not rely on the legacy
`ehbp_output_token_reservation` / `ehbp_output_token_reservation_total_cap` flags.
Those belong to preserved, separately disabled Responses-only scaffolding
(`EHBP_OUTPUT_TOKEN_RESERVATION_ENABLED = False`). If both scaffolds are enabled
in tests, the route list is their union; legacy flags do not establish the new
single-choice contract. By default `/v1/info` still omits `features` entirely.

#### Reservation shape (only after verified activation)

Input and output share the context window. With input hidden, the hold keeps
the worst-case input allowance, not a guessed prompt length:

```
window = top_provider.context_length else model.context_length
W = max((window - cap) * prompt_price + cap * completion_price,
        window * prompt_price)
reserve = max(min_request_msat, min(max_cost_for_model, ceil(W * tol_factor)))
```

A completion-only metadata limit is not the context window. Unknown windows or
unusable prices keep the full envelope. The all-input term prevents discounting
hidden input when input is more expensive. Billing still settles from trusted
`X-Tinfoil-Usage-Metrics`, not the authorization hold.

## End-to-end flow

```
SDK                                  Routstr Proxy                    PPQ.AI /private/
  │                                       │                                │
  │── X-Routstr-Model: tinfoil-kimi-k2-6 ─│                                │
  │── Ehbp-Encapsulated-Key: <hex> ───────│                                │
  │── Authorization: Bearer <cashu> ──────│                                │
  │── body = HPKE-encrypted(kimi-k2-6) ───│                                │
  │                                       │                                │
  │                                  detects Ehbp-Encapsulated-Key         │
  │                                  reads model from X-Routstr-Model      │
  │                                  does billing/routing                  │
  │                                       │                                │
  │                                  adds X-Private-Model: private/kimi-k2-6
  │                                  forwards raw body to /private/v1/...  │
  │                                       │──────────────────────────────▶│
  │                                       │                          enclave decrypts
  │                                       │                          runs inference
  │                                       │◀── Ehbp-Response-Nonce ──────│
  │                                       │◀── encrypted response ────────│
  │                                       │                                │
  │                                  streams response back untouched       │
  │◀── encrypted response ────────────────│                                │
  │                                        │                                │
  SecureClient reads nonce, decrypts       │                                │
  SDK SSE processing sees plaintext        │                                │
```

## Model ID mapping

Three parties see three different model IDs:

| Party | Header/Body | Value | Source |
|---|---|---|---|
| Routstr proxy | `X-Routstr-Model` header | `tinfoil-kimi-k2-6` | SDK sends full caller-facing id |
| Tinfoil usage metrics | `model` field | `kimi-k2-6` | Enclave reports the model actually served |
| Tinfoil enclave | `body.model` (encrypted) | `kimi-k2-6` | SDK strips `tinfoil-` prefix before encryption |

## Implementation status

A dedicated `TinfoilUpstreamProvider` (`routstr/upstream/tinfoil.py`) now
implements the direct blind-upstream pattern described above. The shared EHBP
helpers in `routstr/upstream/ehbp.py` were extended to:

- Request usage metrics via `X-Tinfoil-Request-Usage-Metrics: true`.
- Parse `X-Tinfoil-Usage-Metrics` from the response header (non-streaming) or
  HTTP trailer (streaming).
- Override the forwarding URL with a validated `X-Tinfoil-Enclave-Url` when the
  SDK sends it.
- Finalize bearer billing with the dedicated EHBP actual-cost finalizer.
- Compute X-Cashu refunds from actual cost instead of max cost.
- Relay encrypted enclave error responses verbatim — status,
  `Ehbp-Response-Nonce` and ciphertext body — instead of wrapping the
  ciphertext in a proxy JSON envelope, so the EHBP client can decrypt the
  provider's actual error.

See `docs/tinfoil-direct-integration.md` for the full implementation notes.

The reservation discount keyed on `X-Tinfoil-Max-Output-Tokens` is described
under [Output-cap reservations](#output-cap-reservations--scaffolded-not-active).

## Verification status

Unit coverage includes usage parsing, target validation, HTTP trailer capture,
response-size limits, and bearer payment finalization. End-to-end requests have
verified both non-streaming usage headers and streaming usage trailers against
Tinfoil. SDK behavior on proxy-generated non-2xx responses without an
`Ehbp-Response-Nonce` still merits explicit end-to-end coverage.
