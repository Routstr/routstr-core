# Tinfoil / PPQ Private-Mode Integration Notes

This document summarizes the current options for integrating Tinfoil/PPQ private models with Routstr, based on the EHBP work in this branch and local testing against `ppq-private-mode-proxy`.

## Background

PPQ private models run behind a Tinfoil/EHBP flow:

- Request bodies are HPKE-encrypted by a Tinfoil client.
- The PPQ `/private/` endpoint routes ciphertext to the attested enclave.
- The enclave decrypts, runs inference, and returns an encrypted response.
- The caller's Tinfoil client decrypts the response locally.

The PPQ private-mode proxy (`~/projects/ppq-private-mode-proxy`) uses this pattern with the JavaScript `tinfoil` SDK:

```ts
import { SecureClient } from "tinfoil";

const apiBase = "https://api.ppq.ai";

const client = new SecureClient({
  baseURL: `${apiBase}/private/`,
  attestationBundleURL: `${apiBase}/private`,
  transport: "ehbp",
});

await client.ready();

const response = await client.fetch(`${apiBase}/private/v1/chat/completions`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: `Bearer ${process.env.PPQ_API_KEY}`,
    "X-Private-Model": "private/gpt-oss-120b",
    "x-query-source": "api",
  },
  body: JSON.stringify({
    model: "gpt-oss-120b", // enclave-internal model id
    messages: [{ role: "user", content: "Hello" }],
  }),
});

const json = await response.json();
console.log(json.usage);
```

`X-Private-Model` carries the PPQ-facing private model id, while the encrypted JSON body uses the enclave-internal model id without the `private/` prefix.

## PPQ private model pricing

PPQ exposes private model pricing through:

```text
GET https://api.ppq.ai/v1/models?type=all
```

Filter models whose IDs start with `private/`.

Example private pricing observed:

| Model | Input USD / 1M tokens | Output USD / 1M tokens |
|---|---:|---:|
| `private/gpt-oss-120b` | `0.79125` | `1.31875` |
| `private/llama3-3-70b` | `1.84625` | `2.90125` |
| `private/qwen3-vl-30b` | `1.31875` | `4.22` |
| `private/glm-5-2` | `1.5825` | `5.53875` |
| `private/gemma4-31b` | `0.47475` | `1.055` |
| `private/kimi-k2-6` | `1.5825` | `5.53875` |

The `ppq-private-mode-proxy` commit `ba984214793d3bca0f7d046b6955d42abc1c6843` changed OpenClaw display metadata to align with a 5% API margin. The live PPQ model endpoint returns the more precise rates above, which already include that margin.

Actual PPQ private billing is:

```text
price_usd =
  input_tokens  * input_per_1M_tokens  / 1_000_000
+ output_tokens * output_per_1M_tokens / 1_000_000
```

A test request to `private/gpt-oss-120b` produced:

```json
{
  "input_count": 74,
  "output_count": 3,
  "price_in_usd": 0.00006250875
}
```

which exactly matches:

```text
74 * 0.79125 / 1_000_000 + 3 * 1.31875 / 1_000_000
= 0.00006250875 USD
```

## Integration architectures

There are three materially different ways Routstr could integrate Tinfoil/PPQ private inference.

## Option A: User integrates Tinfoil directly

```text
User app / SDK
  -> Tinfoil SecureClient / TinfoilAI
    -> EHBP-encrypted request
      -> Tinfoil/PPQ private enclave
```

Properties:

- Best privacy for the user.
- Routstr is not in the request path.
- User's client encrypts requests and decrypts responses.
- Usage is visible to the user's app after decryption.
- Billing is handled directly by Tinfoil/PPQ.

Example with Tinfoil's OpenAI-compatible client:

```ts
import { TinfoilAI } from "tinfoil";

const client = new TinfoilAI({
  apiKey: process.env.TINFOIL_API_KEY,
  transport: "ehbp",
});

const res = await client.chat.completions.create({
  model: "llama3-3-70b",
  messages: [{ role: "user", content: "Hello" }],
});

console.log(res.choices[0].message.content);
console.log(res.usage);
```

This is not a Routstr marketplace flow unless Routstr only acts as discovery/UI around direct Tinfoil/PPQ usage.

## Option B: Routstr integrates Tinfoil as an upstream client

```text
User -> Routstr plaintext request
  -> Routstr Tinfoil SecureClient encrypts to PPQ/Tinfoil
    -> PPQ private enclave
  -> Routstr receives decrypted response
  -> Routstr bills from decrypted usage
  -> Routstr returns plaintext response to user
```

Properties:

- Easier exact billing.
- Routstr can read the decrypted OpenAI response and `usage` object.
- Routstr can charge exact PPQ token pricing.
- Privacy is different: the user sends plaintext to Routstr, and Routstr sees prompts/responses.
- End-to-end encryption is only Routstr-to-enclave, not user-to-enclave.

This should be considered a separate product/provider mode, not the same as an end-to-end private relay.

Practical implementation options:

1. Run a Node sidecar that uses the `tinfoil` npm package and expose it as a local HTTP upstream to Routstr.
2. Port EHBP client behavior to Python.
3. Reuse or adapt `ppq-private-mode-proxy` as a local upstream.

A Node sidecar is probably the quickest implementation path because `ppq-private-mode-proxy` already demonstrates the full flow.

## Option C: Routstr uses Tinfoil as a direct blind upstream

This is the current branch's design intent and is likely the best fit if Tinfoil/PPQ exposes usage metadata headers:

```text
User Tinfoil SecureClient
  -> encrypted request body
    -> Routstr proxy
      -> Tinfoil/PPQ private API
         with X-Tinfoil-Request-Usage-Metrics: true
        -> PPQ private enclave
      <- encrypted response
         plus X-Tinfoil-Usage-Metrics / cost headers
    <- Routstr proxy
  -> user decrypts response
```

In this mode Routstr is still a normal upstream proxy from the user's point of view, but the upstream is Tinfoil/PPQ private inference and the body remains opaque to Routstr.

Properties:

- Strongest privacy with Routstr in the path.
- Routstr never sees plaintext prompt or plaintext response.
- Routstr can authenticate and route based on plaintext headers.
- Routstr should request Tinfoil usage metadata by adding `X-Tinfoil-Request-Usage-Metrics: true` to the upstream request.
- Tinfoil documents `X-Tinfoil-Usage-Metrics` as an upstream response header for non-streaming requests.
- For streaming requests, Tinfoil documents `X-Tinfoil-Usage-Metrics` as an HTTP trailer available only after the response body completes.
- If Tinfoil/PPQ returns `X-Tinfoil-Usage-Metrics` or a cost header on the encrypted response, Routstr can bill exactly without decrypting the body.
- If usage is only present inside the encrypted response body, Routstr still cannot read it and exact billing is not possible without a separate metadata path.

This is the only architecture that preserves end-to-end encryption from the user to the PPQ/Tinfoil enclave while still letting Routstr mediate payment. The key requirement is that usage/cost metadata must be returned outside the encrypted body, ideally as a response header available before body streaming begins.

## Current Routstr problem

The current EHBP implementation charges successful EHBP requests at `max_cost_for_model` because Routstr cannot decrypt the response body:

```text
successful EHBP request -> charge full reserved max cost
```

That is incorrect for PPQ private models. Max cost should be only a reservation/solvency ceiling. Final charge should use actual PPQ private token pricing.

Desired behavior:

```text
reserve max cost
forward encrypted request
obtain actual usage/cost metadata
finalize actual cost
refund/release the difference
```

## Usage/cost metadata requirement

For blind-relay exact billing, PPQ should return one of the following outside the encrypted response body:

```http
X-PPQ-Cost-USD: 0.00006250875
```

or:

```http
X-Private-Usage-Metrics: input=74,output=3
```

or:

```http
X-Tinfoil-Usage-Metrics: prompt=74,completion=3,total=77
```

Tinfoil proxy documentation references a usage-metrics flow where a proxy can request usage via:

```http
X-Tinfoil-Request-Usage-Metrics: true
```

and read usage from:

```http
X-Tinfoil-Usage-Metrics
```

Docs/example references:

- https://docs.tinfoil.sh/guides/proxy-server
- https://github.com/tinfoilsh/encrypted-request-proxy-example

During local PPQ testing, PPQ responses included this CORS exposure header:

```http
Access-Control-Expose-Headers: Ehbp-Response-Nonce, X-Private-Usage-Metrics, X-Encrypted-Usage-Metrics, X-Tinfoil-Usage-Metrics
```

However, the actual tested non-streaming response did not include any of these usage headers, even when `X-Tinfoil-Request-Usage-Metrics: true` was sent.

The decrypted body did include normal OpenAI usage, but only the decrypting Tinfoil client can see that body.

## Query-history fallback

PPQ's query history endpoint exposes actual usage and cost:

```text
GET https://api.ppq.ai/queries/history?page=1&page_count=...
```

A record includes:

```json
{
  "timestamp": "...",
  "model": "private/gpt-oss-120b",
  "input_count": 74,
  "output_count": 3,
  "price_in_usd": 0.00006250875,
  "query_type": "chat_completion",
  "query_source": "api"
}
```

This could be used as a fallback, but it is less robust than response headers/trailers because matching a request to a history row can be race-prone under concurrency. It would need a reliable request identifier or metadata field that PPQ stores in history.

## Recommended Routstr direction

Use Tinfoil/PPQ as a direct blind upstream and have Routstr explicitly request usage metadata:

```text
User encrypts body
Routstr reserves max cost
Routstr forwards encrypted body to Tinfoil/PPQ /private/
Routstr includes X-Tinfoil-Request-Usage-Metrics: true
Tinfoil/PPQ returns X-Tinfoil-Usage-Metrics as a response header for non-streaming,
or as an HTTP trailer after the body completes for streaming
Routstr finalizes exact charge
User decrypts encrypted response
```

This preserves both:

- privacy: Routstr cannot read prompts/responses;
- exact billing: Routstr can charge actual PPQ private model cost, assuming Tinfoil/PPQ returns usage/cost metadata outside the encrypted body.

Implementation steps:

1. Update PPQ model fetching to include private models:

   ```text
   GET https://api.ppq.ai/v1/models?type=all
   ```

2. Register `private/*` models and any Routstr-facing aliases with correct `forwarded_model_id`.

3. Keep max-cost reservation for bearer keys and X-Cashu solvency checks.

4. Add parsing support for possible usage/cost headers:

   ```http
   X-PPQ-Cost-USD
   X-Private-Usage-Metrics
   X-Tinfoil-Usage-Metrics
   X-Encrypted-Usage-Metrics
   ```

5. Finalize by actual cost instead of max cost:

   ```text
   actual_msats = ceil((actual_usd / sats_usd_price()) * 1000)
   actual_msats = max(actual_msats, settings.min_request_msat)
   actual_msats = min(actual_msats, reserved_msats)
   ```

   or, if only token counts are available:

   ```text
   actual_usd =
     input_tokens  * input_per_1M_tokens  / 1_000_000
   + output_tokens * output_per_1M_tokens / 1_000_000
   ```

6. If usage/cost metadata is missing, choose an explicit policy:

   - fail closed and refund/revert;
   - query PPQ history as a fallback;
   - fallback to max-cost billing only if explicitly configured and clearly disclosed.

Silent max-cost billing should not be the default for PPQ private requests.

## X-Cashu consideration

For bearer-auth requests, finalization can happen after the response stream completes if usage is delivered as a trailer.

For `X-Cashu`, Routstr needs to return the refund token in the response headers. If usage/cost is only available after consuming the encrypted response stream, Routstr may need to buffer EHBP responses before sending them to the client so it can compute the refund amount first.

Possible approaches:

1. Prefer a non-trailer response header with actual cost, available before streaming body starts.
2. Buffer EHBP X-Cashu responses and then return `X-Cashu` refund.
3. Introduce a later/refund-claim mechanism, which would be a larger protocol change.

## Summary

- PPQ private models are billed per actual input/output tokens.
- Private model rates are available from `GET /v1/models?type=all`.
- Current Routstr EHBP billing at max cost is wrong for PPQ private models.
- Direct Tinfoil integration inside Routstr would enable exact usage billing but would make Routstr see plaintext.
- A blind EHBP relay preserves privacy but requires PPQ/Tinfoil to expose usage/cost in plaintext headers/trailers.
- The preferred solution is to keep Routstr blind and have PPQ return billing metadata outside the encrypted body.

## Implementation status

Direct Tinfoil upstream integration is implemented in `routstr/upstream/tinfoil.py`
and `routstr/upstream/ehbp.py`.

### What was built

- `TinfoilUpstreamProvider` (`provider_type = "tinfoil"`):
  - Base URL: `https://inference.tinfoil.sh`
  - Fetches models from the public `GET /v1/models` endpoint (no auth needed).
  - Parses Tinfoil's pricing (`inputTokenPricePer1M`, `outputTokenPricePer1M`,
    `requestPrice`) into the standard `Model`/`Pricing` schema.
  - `supports_ehbp = True` — acts as a blind EHBP relay.
  - `get_ehbp_forwarding_target()` returns a target that includes
    `X-Tinfoil-Request-Usage-Metrics: true`.
  - `forward_get_request()` proxies `/attestation` to `https://atc.tinfoil.sh/attestation`
    so the SDK can fetch attestation bundles through Routstr.
  - Registered in `routstr/upstream/__init__.py` and seeded from
    `TINFOIL_API_KEY` env var.

- `routstr/upstream/ehbp.py`:
  - `parse_tinfoil_usage_metrics()` parses
    `prompt=N,completion=N[,total=N][,model=<name>]` into an OpenAI-style
    usage dict. The `model` field (added in tinfoilsh/confidential-model-router
    PR #385) is extracted as a string.
  - `_resolve_ehbp_target_url()` overrides the forwarding URL with
    `X-Tinfoil-Enclave-Url` when the SDK sends it.
  - `_strip_proxy_headers()` removes `X-Routstr-Model`,
    `X-Tinfoil-Enclave-Url`, and `X-Tinfoil-Request-Usage-Metrics` before
    forwarding to the enclave.
  - `_compute_ehbp_actual_cost()` converts the usage header into msats via
    `calculate_cost()`, clamped to `[min_request_msat, max_cost_for_model]`.
    When the header's `model=<name>` differs from the requested model, the
    actual served model's pricing is used for cost calculation.
  - `forward_ehbp_request()` (bearer auth): if `X-Tinfoil-Usage-Metrics` is
    present in the response header, finalizes with `adjust_payment_for_tokens()`
    for exact billing; otherwise falls back to max-cost. Billing uses the
    actual served model when it differs from the requested one.
  - `forward_ehbp_x_cashu_request()`: if usage is available, computes the
    refund from actual cost instead of max cost, using the actual served
    model's pricing when applicable.

- `routstr/proxy.py`: `/attestation` and `/tee/attestation` paths are forwarded
  to Tinfoil upstreams without model/cost/auth lookups.

### Billing behavior

| Request shape | Usage source | Billing |
|---|---|---|
| Bearer, non-streaming | `X-Tinfoil-Usage-Metrics` response header | Exact token cost via `adjust_payment_for_tokens` |
| Bearer, streaming | `X-Tinfoil-Usage-Metrics` HTTP trailer | Exact token cost (h11 captures trailers) |
| Bearer, no usage header/trailer | N/A | Max-cost fallback |
| X-Cashu, non-streaming | `X-Tinfoil-Usage-Metrics` response header | Refund = `redeemed - actual_cost` |
| X-Cashu, streaming | `X-Tinfoil-Usage-Metrics` HTTP trailer | Refund = `redeemed - actual_cost` (h11 captures trailers) |
| X-Cashu, no usage header/trailer | N/A | Refund = `redeemed - max_cost` |

### Cost response headers

Since EHBP response bodies are opaque encrypted blobs, per-request cost cannot
be injected into the JSON body (as done in the normal proxy flow). Instead,
Routstr returns cost info as response headers:

| Header | Auth | Description |
|---|---|---|
| `X-Routstr-Cost-Msats` | Bearer, X-Cashu | Total msats charged for this request |
| `X-Routstr-Cost-Usd` | Bearer | USD equivalent of the charge |
| `X-Routstr-Input-Cost-Msats` | Bearer, X-Cashu | msats attributed to input tokens |
| `X-Routstr-Output-Cost-Msats` | Bearer, X-Cashu | msats attributed to output tokens |

The client/Tinfoil SDK can read these headers from the HTTP response without
needing to decrypt the body.

### Setup

```bash
TINFOIL_API_KEY=your-tinfoil-api-key
```

The provider is auto-seeded on first startup.

### Usage metrics header format

Tinfoil returns usage metrics in the `X-Tinfoil-Usage-Metrics` response header
(non-streaming) or HTTP trailer (streaming) when `X-Tinfoil-Request-Usage-Metrics:
true` is sent. As of tinfoilsh/confidential-model-router PR #385, the format is:

```
prompt=<prompt_tokens>,completion=<completion_tokens>,total=<total_tokens>,model=<served_model>
```

The `model` field carries the actual model name served by the enclave.
Routstr uses this to:

- Verify the served model matches the expected upstream model. The comparison
  uses ``model_obj.forwarded_model_id`` (the actual upstream ID, e.g.
  ``glm-5-2``) rather than ``model_obj.id`` (the client-facing alias, e.g.
  ``tinfoil-glm-5-2``), so aliased models don't trigger a spurious mismatch.
- When they genuinely differ (Tinfoil served a different upstream model than
  expected), look up the actual served model's pricing and use it for billing.
  The reverse lookup uses ``get_model_instance``, which resolves
  ``forwarded_model_id`` values registered as routable aliases.
- Log the discrepancy for observability.

If the actual model is not found in Routstr's model registry, billing falls
back to the requested model's pricing.

### What still needs verification

- ~~End-to-end test with a real Tinfoil SDK client against a Routstr node with
  `TINFOIL_API_KEY` set.~~ Verified: both non-streaming (header) and streaming
  (trailer) responses include `model=<name>`.
- Streaming requests: usage is delivered as an HTTP trailer. Currently the
  bearer path finalizes max-cost before streaming begins. Supporting streaming
  usage would require buffering the response (for X-Cashu) or a deferred
  finalization (for bearer).
- Whether Tinfoil's `/v1/responses` endpoint also returns usage metrics
  headers.
