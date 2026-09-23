# Image Generation

Routstr proxies image generation the same way it proxies chat: the client
pays in sats, the request is forwarded to a configured upstream, and the
charge is settled from what the response actually carried.

Supported upstreams: **Venice AI**, **OpenAI**, **OpenRouter**, **Together AI**.
Any of them is added from the dashboard like a text provider; their image
models appear in `/v1/models` with `output_modalities: ["image"]`.

## Endpoints

The OpenAI-compatible route works on every supported upstream:

```
POST /v1/images/generations
POST /v1/images/edits
POST /v1/images/variations
```

Venice's native routes (`/v1/image/generate`, `/image/edit`, `/image/inpaint`,
`/image/upscale`) are forwarded and billed too.

Send `model` and `prompt`; every other field is passed through unchanged, so
upstream-specific parameters (`resolution`, `quality`, `steps`, `aspect_ratio`,
`width`/`height`, `n`, `output_format`, ...) work as documented by the upstream.

## How an image is priced

Each image model carries a **price book** (`image_pricing` on the model row)
in the upstream's raw USD. The book names the **unit** the upstream meters
in. Two flat rates on `pricing` mirror it, following OpenRouter's naming:

- `pricing.image_output`: the ceiling one generated image can cost. The
  provider fee and the sats conversion apply to it like any other rate, and
  every tier in the book is a ratio of it.
- `pricing.image`: USD per **input** image, the surcharge for each reference
  image a request attaches (`input_references`, `reference_images`,
  `image_url`, `style_references`, `image`). Charged once per request, past
  any images the upstream includes for free. The same field prices vision
  attachments on chat models.

| Unit | Reserved on request | Settled from response | Upstreams |
| --- | --- | --- | --- |
| `image` | requested resolution/quality tier × `n` | images returned × tier price | Venice, Together (most), OpenRouter (most) |
| `token` | documented per-image estimate for the tier × `n` | `usage.output_tokens` × image-output rate, plus input tokens | OpenAI GPT Image, OpenRouter (gpt-image) |
| `megapixel` | requested output area × rate × `n` | images returned × requested area × rate | Together FLUX, OpenRouter (BFL) |

When the book has `trust_upstream_cost` (OpenRouter), the response's
`usage.cost` in USD settles the charge exactly, whatever the unit. Otherwise
the reference-image surcharge is added to the flat per-image charge.

A response that returns no image is never charged; the reservation is released.
A request naming a tier the model does not declare is reserved at the ceiling.

### Where books come from

- **Venice** publishes a full book per model on `GET /models?type=all`
  (per resolution, per quality, upscale factors, `inputImages` surcharge past
  the included count). Nothing to configure.
- **OpenRouter** itemises billable lines per endpoint on its Image API
  (`GET /api/v1/images/models/{id}/endpoints`): `output_image` per `image`,
  `megapixel` or `token`, with `variant`s such as `2k` or `medium_1k` that
  become resolution/quality tiers, and `input_image` per image as the
  reference surcharge. Image-only models are missing from the default
  `/models` listing, so the catalog fetch also reads
  `/models?output_modalities=image`. When the Image API is unavailable the
  catalog's `image_output` per-token rate stands in. Settlement always uses
  the reported `usage.cost`.
- **OpenAI** meters image output tokens. The book carries the token rates from
  the OpenRouter catalog and the per-image estimates from OpenAI's image guide
  (per quality at 1K) for the reservation. `xhigh`/`max` on GPT Image 2.5 and
  sizes above 1K are undocumented and reserved at multiples of `high`; the
  charge is exact from `usage`.
- **Together** lists no image prices on `/models`. Models are priced from a
  table of published prices (`routstr/upstream/together.py`). Image models
  missing from that table are **not listed** until you price them:

```json
{
  "image_prices": {
    "black-forest-labs/FLUX.2-dev": {"usd": 0.025, "unit": "megapixel"},
    "some-org/new-model": 0.05
  }
}
```

Put that JSON in the provider's **Settings** field in the dashboard (or the
`provider_settings` column). A bare number is USD per image; `unit` may be
`image` or `megapixel`. Operator prices override the built-in table.

### Per-model overrides

The admin models API accepts `image_pricing` directly, so a book can be
replaced or hand-written for any model:

```json
{
  "max_usd": 0.12,
  "unit": "image",
  "tiers": [
    {"resolution": "1K", "usd": 0.04},
    {"resolution": "2K", "usd": 0.12}
  ],
  "default_resolution": "1K",
  "resolutions": ["1K", "2K"]
}
```

## Adding another upstream

1. Build an `ImagePricing` per image model in the provider's `fetch_models`
   (see `routstr/upstream/image_catalog.py` for the OpenRouter, OpenAI and
   static builders) and pass the books through `attach_image_books`.
2. Pick the unit the upstream meters in and set `trust_upstream_cost` if its
   response reports USD.
3. If the response's usage dialect is new, extend `read_image_response` in
   `routstr/upstream/image_generation.py`.

Reservation, settlement, the provider fee and the sats conversion are shared;
a provider only describes prices.
