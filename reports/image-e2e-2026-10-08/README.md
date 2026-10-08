# Image generation e2e — 2026-10-08 (PR #773)

Prompt (same for every model):

> Minimal flat vector logo for a platform that bundles every kind of organization structure: hierarchies, teams, networks, circles. Abstract mark made of interlocking nodes and layers forming one unified shape, two-tone navy and teal, white background, no text, centered, clean edges.

All requests went through a local routstr-core node on this branch. Charge = wallet balance delta; expected = upstream USD × the model's sats/USD rate (provider fee included).

| File | Provider | Route | Model | Upstream USD | Charged | Expected |
|---|---|---|---|---|---|---|
| openrouter-recraft-v4.1-flash-1.webp | OpenRouter | `/v1/images/generations` | recraft-v4.1-flash | 0.007 | 8.432 sats | 8.431 |
| openrouter-seedream-5-0-flash-1.jpg | OpenRouter | `/v1/images/generations` | seedream-5-0-flash | 0.018 | 21.681 sats | 21.680 |
| openrouter-qwen-image-3-1.png | OpenRouter | `/v1/images/generations` | qwen-image-3 (1K) | 0.030 | 36.134 sats | 36.134 |
| venice-sd35-variant-1.webp, -2.webp | Venice | `/v1/image/generate` `variants: 2` | venice-sd35 | 2 × 0.01 | 24.083 sats | 24.083 |
| venice-upscaler-2x.png | Venice | `/v1/image/upscale` `scale: 2` | upscaler | 0.02 (upscale 2x) | 24.083 sats | 24.083 |
| together-seedream-3.0-1.jpg | Together | `/v1/images/generations` 1024×1024 | Seedream-3.0 | 0.018 | 21.965 sats | 21.747 |
| together-flux.2-dev-1.jpg | Together | `/v1/images/generations` 1024×1024 | FLUX.2-dev | 0.0154 per image | 18.792 sats | 18.6 |
| together-flux.1-kontext-pro-1.jpg | Together | `/v1/images/generations` 1024×1024 | FLUX.1-kontext-pro | 0.04 per MP × 1.05 MP | 51.181 sats | 50.7 |

The last two rows exercise the review fixes: `variants` is reserved and billed like `n`, and an upscale is priced by its factor instead of the model's generation tier.

Together prices come from its `/models` feed (`image.example_price` per image, `image_pixel.price_per_megapixel` per megapixel), cross-checked against together.ai/pricing: FLUX.2 [dev] $0.0154 per image, Seedream 3.0 $0.018 per image, FLUX.1 Kontext [pro] $0.04 per MP. Together's image models are all passthrough third-party models; the org needs "Allow passthrough models" on (Organization Settings, Privacy) or every call returns `third_party_data_sharing_blocked`. Text chat through the Together provider was also verified (qwen3.5-35b-a3b, qwen3-coder-30b-a3b-instruct, qwen3-30b-a3b, gemma-4-26b-a4b-it: 200, charged 2 to 7 msat each, matching the balance delta).
