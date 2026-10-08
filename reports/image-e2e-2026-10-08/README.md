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

The last two rows exercise the review fixes: `variants` is reserved and billed like `n`, and an upscale is priced by its factor instead of the model's generation tier.

Together: provider registered, catalog priced (21 image models), but every generation on this account returns `third_party_data_sharing_blocked`; enable third-party data sharing on the Together org to run them.
