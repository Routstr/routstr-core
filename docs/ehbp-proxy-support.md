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

Contains the shared opaque EHBP transport helpers:

- `EHBPForwardingTarget` — provider-specific target URL plus extra headers
- `forward_ehbp_request()` — forwards the raw encrypted body to an EHBP-capable
  provider, streams the encrypted response back untouched, and finalizes bearer
  billing at max cost because usage is encrypted
- `forward_ehbp_x_cashu_request()` — redeems the Cashu token, forwards raw,
  refunds the full token on upstream failure, and refunds any value above
  `max_cost_for_model` on success

### `routstr/upstream/ppqai.py`

- Sets `supports_ehbp = True`.
- Implements `get_ehbp_forwarding_target()` to forward to
  `https://api.ppq.ai/private/v1/...` — the PPQ.AI enclave endpoint that
  understands EHBP and returns the `Ehbp-Response-Nonce` header.
- Adds `X-Private-Model` with the model's `forwarded_model_id` (e.g.
  `private/kimi-k2-6`). PPQ.AI's billing layer needs this since it can't
  decrypt the body.

## Why it's done this way

The proxy is a **blind relay** for EHBP requests. It cannot decrypt the body
(only the attested enclave can), so it must:

1. Get the model ID from a header, not the body
2. Forward the raw bytes without parsing or transformation
3. Stream the response back without SSE/cost parsing
4. Pass through EHBP protocol headers (`Ehbp-Encapsulated-Key` on request,
   `Ehbp-Response-Nonce` on response)

Cost tracking happens at the proxy level using `max_cost_for_model` from the
model registry. Because EHBP responses are encrypted, Routstr cannot reconcile
against token usage. Bearer requests reserve and then finalize max-cost billing;
X-Cashu requests redeem the token and refund any amount above max cost.

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
| PPQ.AI billing | `X-Private-Model` header | `private/kimi-k2-6` | Proxy sends `forwarded_model_id` |
| Tinfoil enclave | `body.model` (encrypted) | `kimi-k2-6` | SDK strips `tinfoil-` prefix before encryption |

## Not yet tested

These changes were written without integration testing due to the complexity
of the full stack (SDK + proxy + PPQ.AI enclave + Cashu mint). Needs end-to-end
verification with a real `tinfoil-*` model request.

Important assumptions to verify:

- PPQ.AI accepts `/private/v1/...` with `X-Private-Model`.
- PPQ.AI enforces consistency between `X-Private-Model` and the encrypted
  `body.model`, otherwise a malicious client could understate
  `X-Routstr-Model` for billing.
- SDK behavior on non-2xx proxy-generated errors that do not carry
  `Ehbp-Response-Nonce`.
