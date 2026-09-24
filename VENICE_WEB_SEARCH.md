# Venice web search through Routstr `/v1/messages`

Status: **implemented on branch `feat/venice-provider`** and **verified against live Venice** (2026-09-25, see "Live verification"). Investigated 2026-09-24, built 2026-09-25. The production request body and a live Venice credential were unavailable; distinguish reproduced local behavior from the inferred production trigger below.

## What shipped

Two commits on `feat/venice-provider` (branched from `main`):

- `f77896f1` `feat: add venice upstream provider` — `VeniceUpstreamProvider` ported text-and-embedding only from `feat/venice-provider-image-pricing`. Image, inpaint and upscale families are dropped rather than listed, because their price book lives in the image-billing commit that did not come along; listing them here would hand out unpriced inference.
- `5d1004d3` `feat: translate anthropic web search to venice search on /v1/messages` — the fix below.

**The seam.** `BaseUpstreamProvider.adapt_messages_request(body, model_obj) -> str` is a provider's last word on an allowlisted Anthropic body: it may rewrite the body in place and returns a suffix for the upstream model name. `dispatch_anthropic_messages` calls it after the `ALLOWED_MESSAGES_REQUEST_FIELDS` filter and appends the suffix to `transform_model_name(model.id)`. The base implementation returns `""`, so no other provider changes shape.

**The Venice override.** Any tool litellm would read as web search — `type` starting `web_search`, or `name == "web_search"`, the same two markers its adapter matches — is lifted out of `tools`, and the intent is re-expressed as the model feature suffix `:enable_web_search=auto&enable_web_citations=true`. `auto` matches Anthropic semantics, where declaring the tool leaves the decision to the model. Citations are requested because litellm's Anthropic response translation carries no `venice_parameters`, so inline `[REF]n[/REF]` markers are the only surviving signal of which sources were used. Remaining function tools and their `tool_choice` travel untouched; when the search tool was the only one, `tool_choice` is dropped with it, since an OpenAI-shaped upstream rejects a choice with no tools.

**Refusals.** `max_uses`, `allowed_domains`, `blocked_domains` and `user_location` have no Venice equivalent, and a `tool_choice` naming `web_search` cannot be honoured because Venice's search is not a callable tool. Each returns 400 `UNSUPPORTED_WEB_SEARCH_OPTION` before the upstream call rather than a search that quietly ignored the constraint. A key carrying `null` or `[]` states no constraint and is read as absent.

**Verification.** `tests/unit/test_venice_web_search.py` (13 tests) covers the adaptation, the refusals, and — running the real litellm adapter — asserts the unadapted body derives `web_search_options` while the adapted one does not. `tests/integration/test_venice_web_search_wire.py` runs the whole dispatch against a loopback OpenAI-compatible server and reads the bytes Venice would receive: `POST /v1/chat/completions`, no `web_search_options`, `model` carrying the suffix, the function tool in OpenAI shape. Full unit suite 1722 passed; ruff and mypy clean.

That wire test also pins a dependency on startup config: without `configure_litellm()` (applied in `routstr/core/main.py`), litellm posts the Anthropic body to `/responses`, which Venice serves only in alpha.

## Live verification

Run 2026-09-25 against `api.venice.ai` with a real key. Each open question from the plan is now answered by observation rather than inference.

**The suffix is honoured.** A `/v1/messages` request carrying an Anthropic `web_search_20250305` tool came back with a live figure and its source ("approximately $84,216.93 USD, according to CoinMarketCap.^6^") on `deepseek-v4-flash-0731`, and the same on `zai-org-glm-5-1` over a real stream. No 400. The control request without the tool searched nothing.

**Streaming is intact.** The stream yields the full Anthropic event set — `message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop` — with usage on the final events.

**Citations arrive as `^n^`, not `[REF]n[/REF]`.** The API reference describes the latter; live responses write superscript markers, matching Venice's own agent skill. Structured citations are confirmed lost: the Anthropic-shaped response carries only `content`, `id`, `model`, `role`, `stop_reason`, `stop_sequence`, `type`, `usage`, with no `venice_parameters`. The inline markers are the whole signal.

**The capability gate is unnecessary.** All 123 text models in the live catalog report `supportsWebSearch: true` — none false, none missing the key. There is no Venice text model to refuse, so the `Model` field, `ModelRow` column and migration the plan called for are not worth building. Revisit only if Venice ships a text model without it.

**`Pricing.web_search = 0.0` is right.** Venice bills search through the prompt: the same question cost 5,839 input tokens with search against 1,710 without, because the results are injected into the context. There is no separate per-search fee to price (Venice documents one only for `enable_x_search`, which this path never enables). Those tokens are billed by the existing per-token path, and `_calculate_usd_max_costs` reserves against the full context window, so an inflated prompt stays inside the reservation.

Still unobserved: behaviour when Venice's search itself fails or returns nothing, and `enable_web_scraping`, which this path never turns on.

## Incident and conclusion

Routstr 0.4.7 logged a `/v1/messages` request (`09adf07c-1456-4ccb-8276-824016392219`) dispatched to `https://api.venice.ai/api/v1` with LiteLLM model `openai/deepseek-v4-flash-0731`. Venice returned HTTP 400: `Unrecognized key(s) in object: 'web_search_options'`. The proxy then logged `provider=generic`, `status_code=400`, `retry=true`.

These labels describe different layers. `generic` is Routstr's provider row; `openai/` selects LiteLLM's OpenAI-compatible Chat Completions adapter, not the destination service. `api_base` still points to Venice. The installed LiteLLM does not recognize `venice/` as a provider prefix, so simply renaming it breaks routing. LiteLLM removes `openai/` when resolving the provider; the *outbound* model ID should be the bare Venice ID. Capture one sanitized outbound request to verify the wire payload rather than relying on the dispatch log.

The 400 is about the **unsupported field**, not the prefix. Routstr allowlists `tools` but does not forward client-supplied `web_search_options`. The installed LiteLLM 1.93.2 Anthropic Messages adapter recognizes a tool whose `type` starts with `web_search` or whose `name` is `web_search`, removes it from ordinary function tools, and inserts `web_search_options: {}` into the OpenAI-shaped call. A local, credential-free repro with `web_search_20250305` produced that exact field; an ordinary function tool did not. The production log has no input `tools` field, so the specific incoming trigger remains **strongly indicated, not proved**. A sanitized copy of the incoming `tools` types/names would settle it.

The shared `litellm.drop_params=True` setting is not sufficient to protect arbitrary OpenAI-compatible servers: the adapter creates this field *after* Routstr filters the incoming body. Similarly, the proxy's `correct_request` retries on client request fields, not on this post-translation field. Its `retry=true` means another candidate provider may be attempted for a 400, not that the same Venice request becomes valid.

## Venice's actual search interfaces

Venice documents **model-integrated web search** for `POST /chat/completions` using `venice_parameters.enable_web_search` (`"off"`, `"auto"`, `"on"`; default `"off"`). `"on"` forces search; `"auto"` leaves it to the model. `venice_parameters.enable_web_citations: true` asks for inline source references. The response may include `venice_parameters.web_search_citations`; citations arrive in the first streaming chunk or the non-streaming response. The model feature suffix is another documented way to set these without an extra request field:

```text
<venice-model-id>:enable_web_search=auto&enable_web_citations=true
```

For standalone retrieval, Venice also has `POST /augment/search` and `/augment/scrape`, but that is a different architecture: Routstr would have to execute search, supply results to the model, handle citations and account for the extra call. Venice model metadata advertises `model_spec.capabilities.supportsWebSearch` for model-specific support; verify the actual configured model at runtime rather than assuming all Venice models support it. The incident alone does **not** prove `deepseek-v4-flash-0731` advertises this capability.

Important documentation discrepancy: Venice's first-party `venice-chat` skill describes `tools: [{"type":"web_search"}]` as a built-in toggle, while the official Chat Completions OpenAPI schema currently says only function tools are supported. Treat the `venice_parameters`/suffix route as the documented baseline; test built-in `tools` on the live API before depending on it. Neither source documents accepting the top-level `web_search_options` field rejected in this incident.

## Routstr implementation plan

1. **Write a red regression at the actual seam.** Extend `tests/unit/test_messages_litellm_dispatch.py` with a generic provider pointing at Venice and an Anthropic `/v1/messages` request containing a server-side `web_search_20250305` tool. Exercise `BaseUpstreamProvider._dispatch_anthropic_messages` through `messages_dispatch.dispatch_anthropic_messages`. Use the real LiteLLM translation in a local, network-free adapter assertion, not only an `acreate` mock: assert that the current path produces `web_search_options` and that the proposed path does not. Cover both bearer-key and x-cashu callers because both use the same dispatcher.
2. **Add a narrowly scoped Venice capability branch** before `litellm.anthropic.messages.acreate` in `routstr/upstream/messages_dispatch.py`, with the provider identity supplied by `BaseUpstreamProvider` (or an explicit provider capability). Match the parsed Venice hostname exactly, not an unbounded substring or a model name; generic non-Venice hosts must remain unchanged. Keep `openai/` as the LiteLLM adapter prefix and `api_base` as Venice. Do not rewrite the public `Model.id` or `forwarded_model_id`.
3. **Translate intent, not merely delete it.** On a Venice route, remove only Anthropic *server-side web-search* tools from the `tools` sent into LiteLLM so its adapter cannot synthesize `web_search_options`. Preserve ordinary function tools and their `tool_choice`. Enable Venice search for this request with the documented suffix on the **upstream** model ID, e.g. `:enable_web_search=auto`, optionally adding `&enable_web_citations=true` if the response path preserves citations. This avoids relying on unknown `extra_body` behavior in LiteLLM's Anthropic adapter. Alternatively, pass `venice_parameters` only after a wire-level test demonstrates it survives that adapter. Never silently remove a requested search tool without enabling an equivalent service.
4. **Make unsupported semantics explicit.** Decide and test how to handle `max_uses`, `allowed_domains`/`blocked_domains`, forced `tool_choice` targeting web search, duplicate search tools, or a model without `supportsWebSearch`: Venice's search switch is not a one-to-one implementation of every Anthropic tool constraint. Where equivalence cannot be guaranteed, return a clear pre-dispatch 4xx or explicitly documented degraded behavior; avoid a success that pretends the requested constraints were enforced. Do not translate client-supplied arbitrary `venice_parameters` through the `/v1/messages` allowlist.
5. **Preserve the API contract.** Test streamed and non-streamed Anthropic-shaped responses, function tools coexisting with search, no-search Venice requests, non-Venice OpenAI-compatible requests, and handling of `venice_parameters.web_search_citations`. The existing LiteLLM → Anthropic response conversion may drop Venice-specific citation metadata; verify it with captured fixtures before promising search citations. If metadata is lost, either map it deliberately to the chosen client-visible format or document that search works without structured citations.
6. **Protect billing and routing.** `GenericUpstreamProvider.fetch_models` currently sets `Pricing.web_search=0.0`; check Venice's live web-search charges and returned usage/cost fields. Ensure reservation/max-cost estimation and final charge include any search fees before enabling paid searches, or fail closed if they cannot be priced. The 400 fallback behavior in `routstr/proxy.py` must not route a search-required request to a provider that silently loses search; inspect candidate capabilities and keep payment reversal correct. Keep the suffix out of catalog IDs, public response model IDs, and price lookups.
7. **Verify live with a Venice test key** after network-free tests: record sanitized outbound JSON and check absence of `web_search_options`, bare upstream model name plus the Venice suffix (if chosen), successful web-enabled reply, citations/usage shape, and billing reconciliation for `stream=true` and `false`. Check model capability from `/models` first. No live request was sent in this investigation.

Acceptance: web-search requests on a Venice model that supports search either complete with search enabled and correctly billed, or fail before the upstream call with a specific unsupported-capability error; no request emits `web_search_options` toward Venice. Requests without search and other providers retain their existing behavior. No unsupported search constraints are silently accepted.

## Related reports and prior art

- [LiteLLM #10714](https://github.com/BerriAI/litellm/issues/10714) and its referenced [#10664](https://github.com/BerriAI/litellm/issues/10664) concern Anthropic `web_search_20250305` support in LiteLLM; these are historical context for adapter differences, **not** a verified patch for this Venice 400.
- [LiteLLM #14250](https://github.com/BerriAI/litellm/issues/14250) documents that even OpenAI Chat Completions web search via `web_search_options` is model-specific; an OpenAI-compatible endpoint need not implement it.
- [LiteLLM web-search interception integration](https://docs.litellm.ai/docs/web_search_interception) is an alternative architecture with an external search provider and an agentic follow-up, not a drop-in change to Routstr's current direct `litellm.anthropic.messages.acreate` path. A [follow-up duplicate-kwargs report](https://github.com/BerriAI/litellm/issues) was found in the broader search but not established as this issue's cause; do not infer a fix from it.
- First-party [Venice Chat skill](https://github.com/veniceai/skills/blob/main/skills/venice-chat/SKILL.md) gives provider-native search examples. The official API reference below takes precedence for the implementable request shape. Searches for an exact public Venice + LiteLLM `web_search_options` 400 fix did **not** yield a verified matching issue or merged patch. Do not claim an upstream fix exists without reproducing it in the pinned version.

## Branch `feat/venice-provider-image-pricing` — a Venice provider class already exists

Checked 2026-09-24 on that branch (two commits ahead of `main`, no PR open). `routstr/upstream/venice.py` adds `VeniceUpstreamProvider(BaseUpstreamProvider)` with `provider_type = "venice"`, a pinned `default_base_url = "https://api.venice.ai/api/v1"` (`fixed_base_url: True`), a catalog fetch across Venice's model families, text and per-image-tier pricing, and `transform_model_name` stripping a `venice/` prefix. Tests in `tests/unit/test_upstream_venice.py` are catalog and pricing only.

It does **not** fix this incident. Verified at runtime on the branch: `VeniceUpstreamProvider.litellm_provider_prefix` is `None`, so `get_litellm_provider_prefix()` still resolves to `openai/` through `detect_litellm_prefix`, and `supports_anthropic_messages` is `False`, so `/v1/messages` still goes through `messages_dispatch` into LiteLLM's Anthropic adapter — the same code that synthesizes `web_search_options`. The file contains no web-search or `venice_parameters` handling.

What it does change is **where the fix belongs**. With this class merged, step 2 of the plan above needs no hostname matching: provider identity is the class itself, so the Venice branch becomes a method on `VeniceUpstreamProvider` rather than a URL test inside the shared dispatcher. Adopt it and revise the plan as follows:

- Put the translation on the provider, e.g. an override of `_dispatch_anthropic_messages` (or a narrow hook the base dispatcher calls) that strips Anthropic server-side web-search tools and enables Venice search. Keep `openai/` as the LiteLLM adapter prefix.
- Do **not** append the `:enable_web_search=…` suffix inside `transform_model_name`. `base.py` calls it on the chat/completions and model-listing paths too (around lines 705, 724, 795), so a suffix there would leak into unrelated requests. Scope it to the messages dispatch call.
- The incident ran on a `generic` row, not this class. Using it means re-creating the Venice upstream row as `provider_type="venice"`; `_build_from_row` takes only `api_key` and `provider_fee` because the base URL is pinned. A stale `generic` row keeps the old behavior.
- `_parse_pricing` returns text `Pricing` without a `web_search` rate (defaults to `0.0`), so per-search charges are still unpriced — the billing item in step 6 stands unchanged.

The branch is unreviewed and carries an unrelated image-generation billing commit (27 files, ~3.9k insertions). Landing the web-search work on top of it couples this fix to that review. Decide explicitly: build on the branch, or implement against `main` and rebase once the provider lands.

## Adding Venice support to LiteLLM

Investigated 2026-09-24 against installed LiteLLM 1.93.2 and upstream `main` (published 1.102.1).

### What already exists

Venice is **already registered** in LiteLLM, but only as a bare JSON entry. `litellm/llms/openai_like/providers.json` contains, on both the pinned version and upstream `main`:

```json
"veniceai": {
  "base_url": "https://api.venice.ai/api/v1",
  "api_key_env": "VENICE_AI_API_KEY"
}
```

Verified locally: `litellm.get_llm_provider("veniceai/deepseek-v4-flash-0731")` resolves to `("deepseek-v4-flash-0731", "veniceai")`, while `venice/...` raises `LLM Provider NOT provided`. `veniceai` is **not** in `litellm.provider_list` or the `LlmProviders` enum — it resolves through `JSONProviderRegistry`, which `get_llm_provider_logic.py` checks before the enum. Upstream `main` has no `litellm/llms/venice*` directory, no Venice entries in `model_prices_and_context_window.json`, and `docs.litellm.ai/docs/providers/venice` returns 404. The `venice` block in the installed `provider_endpoints_support_backup.json` describes a provider that was never merged.

### The JSON entry does not fix this incident

JSON providers inherit `OpenAIGPTConfig`, whose supported-parameter list includes `web_search_options`. Verified locally with the generated config class: `get_supported_openai_params` returns 26 params including `web_search_options`, and `map_openai_params({"web_search_options": {}}, drop_params=True)` keeps the field. So `litellm.drop_params` will not remove it, and switching Routstr's prefix from `openai/` to `veniceai/` still emits the field Venice rejects. `get_optional_params(..., custom_llm_provider="veniceai", extra_body={"venice_parameters": {...}})` does keep `extra_body` alongside `web_search_options`; whether that survives the Anthropic-messages adapter to the wire is **untested**, as no live request was made.

### Prior attempts and maintainer stance

- [#17962](https://github.com/BerriAI/litellm/pull/17962) **merged** — the two-line `providers.json` entry above, one file, no tests.
- [#17948](https://github.com/BerriAI/litellm/pull/17948) **closed unmerged** — a full `VeniceAIChatConfig(OpenAILikeChatConfig)` with a `VENICE_PARAMS` set (`enable_web_search`, `enable_web_citations`, `character_slug`, …) nested into `venice_parameters` by `transform_request`, plus enum, URL detection, docs, and 428 lines of tests. A maintainer replied that provider-specific params already pass through automatically and pointed at the providers.json path; the author closed it in favor of #17962.
- [#18248](https://github.com/BerriAI/litellm/pull/18248) **closed** (stale) — wired `veniceai` into `constants.py`, `types/utils.py`, URL detection, `provider_endpoints_support.json`, and docs.
- [#26970](https://github.com/BerriAI/litellm/pull/26970) (Venice model prices, fixes [#24229](https://github.com/BerriAI/litellm/issues/24229)) and [#23670](https://github.com/BerriAI/litellm/pull/23670) (docs) both **closed unmerged**.
- Feature requests [#8833](https://github.com/BerriAI/litellm/issues/8833) and [#9093](https://github.com/BerriAI/litellm/issues/9093) are closed.

Treat that history as the main risk: the nesting problem this project needs was proposed once and rejected as unnecessary. A new PR must argue what `providers.json` cannot express, rather than restating the request.

### Option A — extend the JSON provider system (recommended upstream path)

`param_mappings` only renames a key; it cannot nest `enable_web_search` under `venice_parameters`, and nothing in the schema can mark an inherited param unsupported. Two small additive fields in `dynamic_config.py` close both gaps generically, for every OpenAI-compatible provider that rejects inherited OpenAI extras:

- `unsupported_params: ["web_search_options"]` — removed from `get_supported_openai_params`, so `drop_params` handles it through the existing path.
- `nest_params_under: "venice_parameters"` with the member list — `map_openai_params`/`transform_request` build the nested object.

Scope: `llms/openai_like/dynamic_config.py`, `providers.json`, `llms/openai_like/README.md`, plus tests under `tests/test_litellm/`. This stays inside the system the maintainer endorsed and benefits other providers, which is the strongest available argument for merge.

### Option B — first-class Python provider

Revive the #17948 + #18248 shape: `litellm/llms/venice_ai/chat/transformation.py`, `LlmProviders.VENICE_AI` in `types/utils.py`, `constants.py` provider list, `api.venice.ai` detection in `get_llm_provider_logic.py`, `__init__.py`/`utils.py` wiring, `ProviderConfigManager` registration, `model_prices_and_context_window.json` (+ backup) from Venice `/models`, `provider_endpoints_support.json`, `docs/my-website/docs/providers/venice.md` + `sidebars.js`, and tests under `tests/test_litellm/llms/venice_ai/`. Contributing requires a signed CLA, at least one test, and a Greptile review request. Only this option can also map an Anthropic `web_search_*` tool to `enable_web_search` inside LiteLLM, and only for callers that reach the chat path with that tool intact.

Both options are upstream work on a third-party project with an uncertain merge outcome and a release lag. Neither removes the need for the Routstr-side plan above, which is the only change that fixes the incident on the pinned 1.93.2.

### If Routstr adopts `veniceai/` later

`detect_litellm_prefix` in `routstr/upstream/litellm_routing.py` would map `api.venice.ai` to `veniceai/`. Gate that on the installed LiteLLM version: the prefix resolves only while the JSON entry exists, it is absent from `litellm.provider_list`, and no Venice model carries LiteLLM pricing, so Routstr's own pricing path stays authoritative. On its own, the prefix change does not stop `web_search_options`.

## Primary sources and local evidence

- [Venice Chat Completions API](https://docs.venice.ai/api-reference/endpoint/chat/completions) — `venice_parameters`, search modes, response citations, strict request schema.
- [Venice Model Feature Suffix](https://docs.venice.ai/api-reference/endpoint/chat/model_feature_suffix) — `<model_id>:<parameter>=<value>` and combined suffixes.
- [Venice Web Search API](https://docs.venice.ai/api-reference/endpoint/augment/search), [Web Search and Scraping guide](https://docs.venice.ai/guides/tools/web-retrieval), [Venice model catalog](https://docs.venice.ai/api-reference/endpoint/models/list).
- Local: `routstr/upstream/litellm_routing.py:24-116`, `routstr/upstream/base.py:361-372,2493-2508`, `routstr/upstream/messages_dispatch.py:59-78,479-531`, `routstr/upstream/generic.py:92-136,209-235`, `routstr/proxy.py:857-923`, `tests/unit/test_messages_litellm_dispatch.py`, `uv.lock` (LiteLLM 1.93.2).
- Installed dependency: `.venv/lib/python3.14/site-packages/litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py:335-351,921-954` creates `web_search_options`; `litellm_core_utils/get_llm_provider_logic.py:206-230` strips the adapter prefix. These locations are version-specific and must be rechecked after dependency upgrades.
- LiteLLM JSON provider system: `llms/openai_like/providers.json`, `json_loader.py`, `dynamic_config.py`, `README.md`; upstream [providers.json on main](https://github.com/BerriAI/litellm/blob/main/litellm/llms/openai_like/providers.json) and [adding OpenAI-compatible providers](https://docs.litellm.ai/docs/contributing/adding_openai_compatible_providers).
