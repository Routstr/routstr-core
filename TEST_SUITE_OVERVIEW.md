# Test Suite Overview: Routstr Core

**~40 test files** split into **unit tests** (25 files) and **integration tests** (11 files + utilities). The suite covers virtually every aspect of the system.

---

## 🔐 Wallet & Authentication

| What's tested | Key files |
|---|---|
| Cashu token receive/send/credit operations | `test_wallet.py` |
| Token mint-swapping (foreign mint → primary mint) with fee estimation, melt failures, edge cases | `test_wallet.py` |
| API key creation from Cashu tokens, duplicate token handling, invalid token rejection | `test_wallet_authentication.py` |
| Authority header validation (missing, malformed, Bearer format, case insensitivity, XSS/nul-byte injection) | `test_wallet_authentication.py` |
| Wallet info endpoints (`/v1/wallet/`, `/v1/wallet/info`) — data consistency, zero balance, expired keys, key isolation | `test_wallet_information.py` |
| Top-up flow: valid tokens, multiple denominations, spent token rejection, concurrent top-ups, zero-amount edge cases, stress tests (50 sequential) | `test_wallet_topup.py` |

---

## 💰 Balance & Billing

| What's tested | Key files |
|---|---|
| Refund endpoint: x-cashu token refund, API-key refund, sat/msat unit handling, swept/collected token edge cases, 404/410/409 errors | `test_balance.py` |
| **Critical bug fix**: Balance never goes negative on cost overrun (when `tolerance_percentage` discounts the reservation and actual cost exceeds it) | `test_balance_negative_on_cost_overrun.py` |
| Concurrent cost overruns: parallel finalizations must never drive balance negative or give free inference | `test_balance_negative_on_cost_overrun.py` |
| Key validity dates, balance limits, daily reset policies, periodic reset jobs, orphan zero-balance key prevention | `test_key_logic.py` |
| Max-cost calculation: known/unknown/disabled models, tolerance percentage discounts, fixed vs token pricing | `test_payment_helpers.py` |
| `CashuTransaction` source field defaults, API-key-sourced transactions | `test_balance.py` |

---

## 🔀 Proxy & Request Routing

| What's tested | Key files |
|---|---|
| POST proxy forwarding (JSON payloads, streaming SSE, non-streaming, content-type preservation, large payloads) | `test_proxy_post_endpoints.py` |
| Model-specific endpoints, malformed JSON, insufficient balance, rate limiting, partial streaming failures | `test_proxy_post_endpoints.py` |
| Concurrent proxy requests (10 concurrent) and database state changes | `test_proxy_post_endpoints.py` |
| 404 catch-all handler: HTML for browsers, JSON for API clients, fallback when UI bundle missing | `test_proxy_not_found.py` |
| Embeddings endpoint and model case-insensitivity | `test_embeddings.py` |
| Model test endpoint: admin auth required, unsupported types rejected, oversized payloads rejected, correct upstream path used | `test_model_test_endpoint_security.py` |

---

## 🌊 Streaming & SSE

| What's tested | Key files |
|---|---|
| **Streaming SSE framing for every provider**: OpenAI-style plain, OpenRouter keepalive comments, comments glued to data chunks, JSON split across TCP boundaries, byte-by-byte fragmentation, Gemini CRLF framing, Azure leading role chunks | `test_streaming_sse_providers.py` |
| **OpenRouter regression**: `: OPENROUTER PROCESSING` keepalive comments must not crash clients with `Unexpected token ':'` | `test_streaming_sse_providers.py` |
| **Gemini combined content+usage chunk regression**: Content delivered when usage is in the same chunk | `test_streaming_sse_providers.py` |
| Mid-stream error events, model name override, multi-line non-JSON `data:` blocks with re-prefixing | `test_streaming_sse_providers.py` |
| Stream ID injection: chat completion IDs and model names injected into streamed chunks | `test_stream_id_injection.py` |

---

## 📨 Anthropic Messages (/v1/messages) Dispatch Path

| What's tested | Key files |
|---|---|
| Litellm-based dispatch for non-native providers, stripping Anthropic-only fields (`thinking`, `output_config`, `cache_control`, etc.) | `test_messages_litellm_dispatch.py` |
| Non-streaming via litellm → returns Anthropic Message response with `cost_sats` | `test_messages_litellm_dispatch.py` |
| Streaming via litellm → emits SSE events + `cost` event at end | `test_messages_litellm_dispatch.py` |
| SSE byte chunks from litellm parsed correctly (split mid-chunk, keepalive comments) | `test_messages_litellm_dispatch.py` |
| x-cashu messages dispatch: non-streaming refund, streaming refund, no-refund when fully consumed | `test_messages_litellm_dispatch.py` |
| Upstream-always-streams + aggregate-on-non-streaming for Fireworks (max_tokens > 4096 workaround) | `test_messages_litellm_dispatch.py` |
| Aggregator: text deltas → single message, tool_use `input_json_delta` concatenation, SSE byte chunk parsing | `test_messages_litellm_dispatch.py` |
| Cost accumulation across multiple message events (uses `+=` not `max()`) | `test_messages_dispatch_cost_accumulation.py` |
| Token/cost extraction, cache tokens, model name extraction/override, SSE encoding, malformed/negative cost clamping | `test_messages_dispatch_cost_accumulation.py` |

---

## 🏷️ Provider Field Injection

| What's tested | Key files |
|---|---|
| Direct upstreams get bare `provider_type`, OpenRouter gets `openrouter:UpstreamProvider`, unknown/missing becomes `"unknown"` | `test_provider_field_injection.py` |
| Idempotency: double-stamping never nests prefix (`openrouter:openrouter:Google` → `openrouter:Google`) | `test_provider_field_injection.py` |
| Whitespace stripping, non-string/non-dict inputs skipped, `inject_cost_metadata` also stamps provider | `test_provider_field_injection.py` |

---

## ⚙️ Upstream Providers

| What's tested | Key files |
|---|---|
| Azure: `api-key` header instead of `Authorization`, BOM-stripped API version, deployment ID path construction, base URL stripping | `test_upstream_azure.py` |
| Gemini messages: `inject_thought_signatures` for tool calls, `_openai_chunks_to_anthropic_events` translator (text, tool_use, [DONE] sentinel, blank lines) | `test_upstream_gemini.py` |
| Routstr upstream: balance RPC (auth header omitted when api_key empty, connect timeout → None), `/v1` path preservation, native messages support | `test_upstream_routstr.py` |
| Error normalization: HTML/plaintext upstream errors → JSON envelope, JSON errors pass through unchanged, empty body handling | `test_upstream_error_response.py` |
| Litellm provider prefix detection: 40+ providers from URL patterns (Fireworks, Groq, xAI, DeepSeek, Together, Perplexity, Mistral, etc.) | `test_litellm_routing.py` |
| Azure ordering beats `api.openai.com`, Ollama localhost detection, casing/trailing slash normalization, custom defaults | `test_litellm_routing.py` |
| Subclass prefix override wins over URL detection, native messages support flags | `test_messages_litellm_dispatch.py` |

---

## 💵 Cost Calculation & Caching

| What's tested | Key files |
|---|---|
| OpenAI vs Anthropic cache token formats (subtractive vs additive), cache_read exceeds prompt_tokens, malformed/boolean/float token coercion | `test_cost_calculation_caching.py` |
| Token field fallback order, missing/null usage blocks, both cache_read and cache_creation simultaneously | `test_cost_calculation_caching.py` |
| x-cashu cost injection in non-streaming/streaming responses, `cost_sats` rounding, existing usage fields preserved | `test_x_cashu_cost_sats.py` |

---

## 🧮 Token Counting

| What's tested | Key files |
|---|---|
| Local `count_tokens` shim: simple messages, litellm fallback, missing model object, empty body, malformed JSON, system prompts, Anthropic system block list, `forwarded_model_id` | `test_count_tokens_local.py` |
| Image token estimation: low/high/auto detail, small/large images, base64, multiple images, `input_image` type, no images | `test_image_tokens.py` |
| Invalid image data falls back to 512×512 defaults | `test_image_tokens.py` |

---

## 🧠 Model Prioritization Algorithm

| What's tested | Key files |
|---|---|
| Cost scores (basic, with request fee, expensive models), provider penalties (regular=1.0, OpenRouter=1.001) | `test_algorithm.py` |
| Model overrides for missing cached models, deduplication by provider identity (not provider type) | `test_algorithm.py` |

---

## 🔄 Reactive Request Correction

| What's tested | Key files |
|---|---|
| Stripping deprecated `temperature`, unsupported params from request body before retry | `test_request_correction.py` |
| No correction when param absent, label already applied, error message doesn't match, empty inputs, non-object body | `test_request_correction.py` |
| Deprecated model name NOT stripped as param, streaming 400 buffered error is correctable | `test_request_correction.py` |
| Sequential two-param correction (with `applied` set guard), immutability of input | `test_request_correction.py` |

---

## 🗄️ Database Consistency

| What's tested | Key files |
|---|---|
| Transaction atomicity: balance update rollback on failure, top-up rollback on network error | `test_database_consistency.py` |
| Concurrent balance updates via direct DB operations, race condition prevention | `test_database_consistency.py` |
| Primary key uniqueness enforced, numeric field constraints | `test_database_consistency.py` |
| Connection pooling under load (50 concurrent requests), index usage (primary key lookup < 10ms) | `test_database_consistency.py` |

---

## 🔍 Nostr Discovery & Analytics

| What's tested | Key files |
|---|---|
| Provider discovery endpoint: default format, `include_json=true`, data structure validation, NIP-91-only parsing | `test_provider_management.py` |
| No-providers, offline providers, duplicate URLs, Nostr relay failures, malformed URLs, parameter validation | `test_provider_management.py` |
| Admin routstr top-up with transient upstream failure retry | `test_provider_management.py` |
| Analytics snapshot payload: top model usage aggregation, schema/shape, fingerprint ignores `generated_at` | `test_nostr_analytics.py` |
| Analytics disable/empty-nsec skip, deduplication of unchanged payloads | `test_nostr_analytics.py` |

---

## ⚙️ Infrastructure & Settings

| What's tested | Key files |
|---|---|
| Settings seed from env, DB precedence over env, unknown key discarding, payout settings defaults and validation | `test_settings.py` |
| Periodic upstream models refresh loop: picks up providers added after startup, disabled at non-positive interval | `test_models_refresh_loop.py` |
| Logging SecurityFilter: Bearer tokens, Cashu tokens, nsec keys, API keys — redacted; case insensitivity, multiple secrets, non-sensitive messages left intact | `test_logging_securityfilter.py` |

---

## 🧪 Integration Test Infrastructure

The `conftest.py` (~400 lines) provides:

- **`TestmintWallet`**: Simulated cashu mint with fallback token creation, secure token uniqueness via `secrets.token_hex` to prevent hash collisions in concurrent tests
- **`DatabaseSnapshot`**: Before/after diffing of API key state (added/modified/removed with field-level deltas)
- **App fixture**: Full FastAPI app with all wallet/proxy mocks patched in (credit_balance, send_token, recieve_token, etc.)
- **Authenticated client**: Client with persistent API key and 10k sat balance created automatically
- **WebSocket mock**: Nostr discovery patched to fail fast for performance
- **Docker vs mock mode**: Switches between real Docker services and in-memory mocks via `USE_LOCAL_SERVICES` env var
