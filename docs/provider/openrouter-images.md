# OpenRouter Images API (opt-in)

Routstr's initial Images API support is a **buffered, safely quoteable subset** of
OpenRouter image generation. It is not complete OpenRouter modality parity.
Existing chat/Responses image input remains on those request surfaces.

## Enable

Configure an OpenRouter provider through the normal provider setup, then set:

```dotenv
IMAGE_GENERATION_ENABLED=true
IMAGE_MAX_REQUEST_USD=0.10
IMAGE_GENERATION_TIMEOUT_SECONDS=120
IMAGE_MAX_RESPONSE_BYTES=41943040
IMAGE_CAPABILITIES_MAX_AGE_SECONDS=3600
```

The feature defaults off, and a zero request ceiling disables it. The USD ceiling
is a **marked-up customer quote admission limit**, not an upstream total-cost
spending cap. Discovery must have fresh Images endpoint capability/pricing data.
Configure the OpenRouter provider through the admin interface using your
`OPENROUTER_API_KEY`, then use a separately funded Routstr account key for client
requests. Keep the OpenRouter key in the node's existing secret/provider
configuration; never put it in a smoke fixture, compose file, image build context,
or client request.

## Request

Send a funded Routstr bearer key to the native endpoint:

```http
POST /v1/images
Content-Type: application/json
Authorization: Bearer <funded-routstr-key>
```

```json
{
  "model": "recraft/recraft-v4.1-flash",
  "prompt": "A simple red circle on a plain white background.",
  "n": 1,
  "aspect_ratio": "1:1",
  "stream": false
}
```

Use an ID advertised by your node. Images initially support **funded bearer/account
authentication only**. Image requests with `X-Cashu` are rejected before redemption
until durable refund-failure recovery is available; Cashu remains supported on
other existing request surfaces. The node pins the upstream provider and disables
fallback; clients must not supply a `provider` object. It does not retry a
dispatched generation after an ambiguous failure.

Image-to-image uses native content parts on endpoints that support and price
references safely:

```json
{
  "input_references": [
    {"type": "image_url", "image_url": {"url": "https://example.com/reference.png"}}
  ]
}
```

Endpoint-specific reference limits apply. HTTP(S) URLs and image base64 data URLs
are accepted according to validation; Routstr does not locally fetch these
references in the Images path. The Recraft smoke model above has no reference
support, so do not add references to that example.

## Billing and limits

- Endpoint pricing retains billable component, unit, variant, and provider.
- Reservations use the safe quote, provider markup, and pinned exchange rate.
- Completed nonempty image results with a valid upstream `usage.cost` can be
  charged even when all token counts are zero. No synthetic chat-token estimate
  is used and the upstream total is not added again to component pricing.
- Failed generation releases this request's account reservation. A missing/invalid
  settlement cost is an error, not permission to bill the full reservation.
- No charge may exceed the reservation; upstream quote violations require
  reconciliation and must not debit unrelated account funds.
- Token-priced models such as `openai/gpt-image-2.5-flare` and megapixel-priced
  endpoints are not enabled until enforced quantity bounds are established.
  Discovery alone does not mean a model is servable.
- Resolution/quality variants are not universally interchangeable. Unsupported
  or ambiguous pricing/parameters fail closed.
- Streaming, EHBP/encrypted bodies, multipart uploads, query parameters,
  provider passthrough, and model-path overrides are blocked initially.
- Native `/v1/images` is not an alias for OpenAI `/images/generations`, `/edits`,
  or `/variations`; those routes remain outside this initial implementation.

The capability metadata is a foundation for additional modalities. Each future
surface still needs its own bounded reservation, response, and settlement
contract before forwarding is enabled.

## Smoke checks

Credential-free fake container instructions are in
`testing-clients/image-smoke/README.md`. This checks the fake API/client contract,
not a complete Routstr container deployment.

The OpenRouter key located in `/home/user/projects/routstr_main/.env` was not used
for this scaffold. Set up the provider through admin before a live check; the
smoke client needs a funded Routstr key, not that OpenRouter key.

The live client defaults to **dry-run**, without network calls or credential
loading:

```sh
python scripts/smoke_images.py
```

For an explicitly approved paid check, provide `ROUTSTR_URL` (ending in `/v1`), a
funded `ROUTSTR_API_KEY`, and `ROUTSTR_IMAGE_PROVIDER_FEE` matching your configured
node/provider markup. An explicitly selected dotenv file is supported; it is
never printed or auto-discovered:

```sh
python scripts/smoke_images.py --env-file /secure/path/smoke.env \
  --execute --max-quoted-usd 0.01
```

This sends at most one generation after a catalogue-price preflight. It only
supports a single fixed output-image price in that smoke path. The budget flag
is a preflight check, **not an upstream spending cap**; configuration and prices
can change between GET and POST. It prints neither keys, image contents, response
bodies nor refund tokens. Avoid running it with shell tracing enabled.

**Verification:** fake-only Podman smoke passed, including cost-only usage and a
valid PNG. Separately, authenticated OpenRouter generation through Routstr's
actual ASGI `/v1/images` route passed on 2026-10-08: Recraft V4.1 Flash returned a
valid 1024×1024 WebP; account settlement charged USD 0.00742 including 6% markup
and released the reservation. That check used disposable SQLite account storage
and the real upstream API, not a full Routstr container deployment. See
`plans/image-modality-support.md` for the verification record and limitations.
