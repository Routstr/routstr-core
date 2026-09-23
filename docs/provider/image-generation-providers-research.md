# Image Generation API Providers — API shape, params, pricing discovery

Reference baseline: Venice `POST /api/v1/image/generate` (https://docs.venice.ai/api-reference/endpoint/image/generate)

Legend: **Sync** = image returned in same response. **Async** = submit + poll/webhook.

---

## 1. Venice AI (baseline)

- Endpoint: `POST https://api.venice.ai/api/v1/image/generate` — Sync. Auth: `Authorization: Bearer <key>`
- Also OpenAI-compatible: `POST /api/v1/images/generations`
- Required: `model`, `prompt` (1–7500 chars, per-model limit in `promptCharacterLimit`)
- Optional: `width`, `height`, `aspect_ratio`, `resolution` (`1K|2K|4K`), `quality` (`low|medium|high`), `steps`, `cfg_scale` (0–20), `seed`, `negative_prompt`, `style_preset`, `style_references[{image,strength}]`, `lora_strength`, `format` (`jpeg|png|webp`), `return_binary`, `variants` (n), `hide_watermark`, `safe_mode`, `embed_exif_metadata`, `enhance_prompt`, `enable_web_search`, `anon_user_id`
- Response: `{ id, images: [base64], timing:{...}, request }`
- Pricing discovery: `GET /api/v1/models` → `model_spec.pricing.resolutions` and `model_spec.pricing.quality[res][quality] = {usd, diem}` (e.g. gpt-image-2 1K low $0.02 / high $0.26, 4K high $0.83).

---

## 2. OpenAI

- `POST https://api.openai.com/v1/images/generations` (Sync) + Responses API `image_generation` tool.
- Models: `gpt-image-2.5-sunburst`, `gpt-image-2.5-flare`, `gpt-image-2`.
- Params: `model`, `prompt`, `n`, `size` (`1024x1024`,`1536x1024`,`1024x1536`,`auto`), `quality` (`low|medium|high|xhigh|max`, gpt-image-2: low/medium/high), `background` (`auto|transparent|opaque`), `output_format` (`png|jpeg|webp`), `output_compression`, `moderation`, `partial_images` (streaming), `user`.
- Response: `data[].b64_json` (+ `usage` tokens).
- Pricing: token-based. GPT Image 2.5: $30/M image output tokens, $8/M image input, $5/M text input. Per-image cost = quality × size token count. Programmatic: no price field in `/v1/models`; use docs pricing page.

## 3. Google Gemini (Nano Banana) — Imagen shut down

- `POST https://generativelanguage.googleapis.com/v1beta/interactions` (or `:generateContent`). Header `x-goog-api-key`.
- Body: `model` (`gemini-3.1-flash-image`), `input[]` (text/image/video parts), `response_format:{type:"image", aspect_ratio:"16:9"}`.
- Sync, returns inline image bytes. Pricing: per-image token flat rate on Gemini pricing page (no price in API).

## 4. Black Forest Labs (FLUX)

- Async. `POST https://api.bfl.ai/v1/flux-2-pro` (also `flux-2-flex`, `flux-pro-1.1`, `flux-kontext-pro`…). Header `x-key`.
- Params: `prompt` (string or structured JSON: subject/background/lighting/style/camera_angle/composition), `aspect_ratio`, `width`/`height`, `seed`, `prompt_upsampling`, `safety_tolerance`, `output_format`, `webhook_url`, `image_prompt`/reference images (up to 10 for editing).
- Returns `{id, polling_url}` → `GET /v1/get_result?id=` until `status: Ready` → `result.sample` (signed URL, expires ~10 min).
- Pricing: pay-as-you-go per image, docs.bfl.ai/pricing; credits via `GET /v1/me` (user credits endpoint).

## 5. Stability AI

- `POST https://api.stability.ai/v2beta/stable-image/generate/{core|ultra|sd3}`. `multipart/form-data`, `Accept: image/*` or `application/json`.
- Params: `prompt`, `negative_prompt`, `aspect_ratio`, `seed`, `output_format` (`png|jpeg|webp`), `style_preset`, `mode`(t2i/i2i), `image`+`strength` for i2i, `model` for sd3.
- Returns raw image bytes or `{image: base64, seed, finish_reason}`.
- Pricing (verified via browser, 1 credit = $0.01, 25 free credits): Stable Image Ultra 8 cr ($0.08), SD3.5 Large 6.5, SD3.5 Large Turbo 4, SD3.5 Medium 3.5, SD3.5 Flash 2.5, Stable Image Core 3, SDXL 1.0 from 0.9. Edit ops: Erase/Inpaint/Remove-BG/Search&Replace 5, Outpaint 4, Replace-BG+Relight 8. Upscale: Fast 2, Conservative 40, Creative 60. Balance: `GET /v1/user/balance`.

## 6. Replicate (aggregator)

- `POST https://api.replicate.com/v1/predictions` (async by default; `Prefer: wait` header → sync). Or `POST /v1/models/{owner}/{name}/predictions`.
- Body: `{version|model, input:{prompt, aspect_ratio, output_format, seed, ...model-specific}, webhook, webhook_events_filter}` → poll `GET /v1/predictions/{id}` until `status=succeeded`, `output` = URL(s).
- Pricing: per output image or per hardware-second, per model. Programmatic: `GET /v1/models/{owner}/{name}` exposes pricing/hardware; e.g. flux-1.1-pro $0.04/image, flux-schnell $0.003/image, ideogram-v3-quality $0.09/image.

## 7. fal.ai (aggregator)

- `POST https://queue.fal.run/{model-id}` e.g. `fal-ai/flux/dev`, `fal-ai/nano-banana-2`. Header `Authorization: Key <key>`.
- Async queue: submit → `{request_id, status_url, response_url}` → poll `GET .../status` (`Queued{position}`/`InProgress{logs}`/`Completed{metrics}`) → `GET .../` result. Also sync `fal.run/`, streaming, WebSocket real-time, webhooks.
- Input per model: `prompt`, `image_size`/`aspect_ratio`, `num_inference_steps`, `guidance_scale`, `num_images`, `seed`, `enable_safety_checker`, `output_format`.
- Output: `{images:[{url,width,height,content_type}], seed, timings}`.
- Pricing: per image or per megapixel (Seedream V4 $0.03/img, Flux Kontext Pro $0.04/img, Nano Banana $0.0398/img, Qwen $0.02/MP). Per-model page lists price; some models GPU-second billed.

## 8. Together AI

- `POST https://api.together.ai/v1/images/generations` (Sync, OpenAI-ish).
- Params: `model` (`black-forest-labs/FLUX.2-pro|dev|flex|max`, `FLUX.1-kontext-pro`), `prompt`, `width`, `height`, `n`, `steps`, `seed`, `negative_prompt`, `guidance`, `image_url` (edit), `reference_images`.
- Response: `{id, model, data:[{index, url}]}`.
- Pricing: per image on pricing page (Seedream 4.0 $0.03, Imagen 4.0 $0.04, Imagen Ultra $0.06, Nano Banana $0.039, Seedream 3.0 $0.018). Extra cost above default steps. `GET /v1/models` returns pricing metadata per model.

## 9. OpenRouter (aggregator, best price discovery)

- `POST https://openrouter.ai/api/v1/images/generations`.
- Params: `model`, `prompt`, `n` (1–10), `resolution` (`512|1K|2K|4K`), `aspect_ratio`, `size`, `quality` (`auto|low|medium|high`), `output_format` (`png|jpeg|webp|svg`), `background`, `output_compression`, `seed`, `stream`, `input_references[]` (i2i), `user`, `provider.only/order`.
- Pricing discovery (best-in-class): `GET /api/v1/models?output_modalities=image` and endpoint listing returns `pricing:[{billable:"output_image", unit:"image"|"megapixel"|"token", cost_usd, variant:"2k"}]` plus `supported_parameters` per provider endpoint.

## 10. xAI (Grok Imagine)

- `POST https://api.x.ai/v1/images/generations` — OpenAI-compatible. Model `grok-imagine-image-2.0`.
- Params: `model`, `prompt`, `n` (≤10), `aspect_ratio`, `resolution`, `response_format` (`url|b64_json`).
- Pricing: flat per generated image; edits bill input + output image. See docs.x.ai pricing page.

## 11. Ideogram

- `POST https://api.ideogram.ai/v1/ideogram-v3/generate` — Sync, `multipart/form-data`, header `Api-Key`.
- Params: `prompt`, `rendering_speed` (`TURBO|DEFAULT|QUALITY`), `aspect_ratio`, `resolution`, `seed`, `num_images`, `magic_prompt`, `style_type`, `negative_prompt`, `style_reference images`, `color_palette`.
- Response: `{created, data:[{url, prompt, resolution, seed, is_image_safe, style_type}]}` (URLs expire).
- Pricing: per image by rendering speed (about.ideogram.ai/api-pricing); via Replicate v3-quality $0.09/img.

## 12. Recraft (verified)

- `POST https://external.api.recraft.ai/v1/images/generations` (+ `/raster`, `/vector` variants). Bearer token.
- Params: `prompt`, `model` (`recraftv4-1`, `recraftv4`, `recraftv3`, `recraftv2`, Flash/Pro tiers), `style`, `substyle`, `style_id`, `style_match` (`regular|precise|flexible`), `size`, `n`, `negative_prompt`, `controls` (colors, artistic_level), `text_layout`, `response_format` (`url|b64_json|multipart` — multipart lowest latency).
- Distinct: true SVG vector output.
- Pricing (verified, 1 API unit = $0.001): V4.1 Flash $0.007/img, V4.1 $0.035, V4.1 Pro $0.21, V4 $0.04, V4 Styles $0.10, V4 Pro $0.25; V3 raster $0.04, V3 vector $0.08; inpaint/outpaint/bg-replace same raster $0.04 / vector $0.08. Prepaid API-unit packages.

## 13. Runware

- Single WebSocket/REST endpoint `https://api.runware.ai/v1`, array-of-tasks body.
- `{taskType:"imageInference", taskUUID, model:"xai:grok-imagine@image-quality", positivePrompt, negativePrompt, width, height, steps, CFGScale, seed, numberResults, outputType, outputFormat, lora[], controlNet[]}`.
- Response includes `imageURL` **and `cost`** per task (built-in cost reporting).
- Pricing discovery (verified, best machine-readable): `GET https://runware.ai/docs/models/index.json` (all models + schema URLs), `GET /docs/models/<model>/schema.json` → `info["x-pricing"] = {currency, rates:[{amount, unit:"durationSecond"|"run"|..., label, display}], examples:[{configuration, price, latencyMs}]}`. SDK: `client.content.getModelPricing('flux-1-dev')` / CLI `runware model pricing <air>` — free, no credits burned.

## 14. Hugging Face Inference Providers

- `POST https://router.huggingface.co/{provider}/...` / task endpoint text-to-image. Bearer `hf_***`.
- Payload: `inputs` (prompt), `parameters:{guidance_scale, negative_prompt, num_inference_steps, width, height, scheduler, seed}`. Returns binary image.
- Pricing: pass-through provider price, billed per request; provider price table on HF docs.

## 15. Other OpenAI-compatible hosts

- **DeepInfra** — `POST https://api.deepinfra.com/v1/openai/images/generations`, per-image pricing on model page.
- **Nebius Token Factory** — OpenAI SDK compatible (`base_url=https://api.studio.nebius.com/v1/`), `images.generate(model, prompt, response_format, extra_body={width,height,num_inference_steps,seed})`.
- **Novita AI** — async: `POST https://api.novita.ai/v3/async/...` (e.g. qwen-image txt2img) → `{task_id}` → `GET https://api.novita.ai/v3/async/task-result?task_id=<id>` (Bearer key). Params: `prompt, negative_prompt, width, height, steps, guidance_scale, seed, image_num, response_image_type`. Per-image price on model page.
- **AI/ML API** — `POST https://api.aimlapi.com/v1/images/generations`, unified across Flux/Imagen/Seedream; token/credit pricing.
- **Leonardo.ai** — async `POST https://cloud.leonardo.ai/api/rest/v1/generations` with `modelId`, `prompt`, `width`, `height`, `num_images`, `guidance_scale`, `alchemy`, `presetStyle` → poll `GET /generations/{id}`. Token-credit pricing per API plan.

---

## Cross-cutting summary

**Three API shapes:**
1. OpenAI-compatible sync (`/v1/images/generations`): OpenAI, Venice, xAI, Together, DeepInfra, Nebius, AI/ML API, Recraft, OpenRouter.
2. Submit + poll/webhook: BFL, Replicate, fal, Novita, Leonardo.
3. Custom sync: Stability (multipart), Ideogram (multipart), Google (interactions), Runware (task array).

**Minimum params everywhere:** `model` + `prompt`. Near-universal optional: size (`width/height` or `aspect_ratio` or `size`), `seed`, `n`/`num_images`, `output_format`, `negative_prompt` (open models), `steps`+`guidance/cfg_scale` (open models only; closed models like gpt-image/Nano Banana use `quality` instead).

**Programmatic price discovery — only these expose it in API:**
- Venice: `GET /v1/models` → `model_spec.pricing.quality`
- Runware: `/docs/models/<model>/schema.json` → `info["x-pricing"]`, or `client.content.getModelPricing()`
- OpenRouter: `GET /api/v1/models` → `pricing[]` with unit/cost_usd/variant
- Runware: `cost` field returned in every response
- Replicate: model object exposes hardware/price
- Together: `GET /v1/models` pricing metadata
Everyone else: static docs pricing page only (OpenAI, Google, BFL, Stability, Ideogram, xAI, Recraft).

**Billing units:** per-image flat (most), per-megapixel (fal Qwen, some OpenRouter), per output token (OpenAI, Gemini), credits (Stability, Leonardo, Ideogram), GPU-second (Replicate/fal custom models).
