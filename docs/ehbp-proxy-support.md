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
  billing at actual cost (falling back to max cost when usage is unavailable)
- `forward_ehbp_x_cashu_request()` — redeems the Cashu token, refunds the full
  token on upstream failure, and refunds the difference between the redeemed
  amount and actual cost (or max cost when usage is unavailable)

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
proxy safely falls back to max-cost billing.

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

See `docs/tinfoil-direct-integration.md` for the full implementation notes.

## Verification status

Unit coverage includes usage parsing, target validation, HTTP trailer capture,
response-size limits, and bearer payment finalization. End-to-end requests have
verified both non-streaming usage headers and streaming usage trailers against
Tinfoil. SDK behavior on proxy-generated non-2xx responses without an
`Ehbp-Response-Nonce` still merits explicit end-to-end coverage.
