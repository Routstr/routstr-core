# Plan: Route Anthropic `/v1/messages` requests to OpenAI-compatible upstreams via litellm

## Problem

Claude Code (and other Anthropic-SDK clients) issues requests against
`/v1/messages`. In `routstr-core`, every request is dispatched through
`BaseUpstreamProvider.forward_request` (or `forward_x_cashu_request`),
which sends the request body verbatim to the upstream's `/messages`
endpoint.

Only two upstream providers natively accept `/v1/messages`:
- `AnthropicUpstreamProvider`
- `OpenRouterUpstreamProvider` (OpenRouter exposes an Anthropic-compat
  endpoint at `/api/v1/messages`)

All other providers expose only `/chat/completions` (OpenAI-compatible).
Today these requests fail with 404 / "endpoint not found" when a Claude
Code user routes them through `routstr-core` to an OpenAI-compatible
upstream.

Affected providers:
`openai`, `groq`, `xai`, `fireworks`, `perplexity`, `gemini`, `ollama`,
`azure`, `ppqai`, `routstr`, `generic`.

## Goal

When the client sends `/v1/messages` and the resolved upstream **does
not** natively support that endpoint, `routstr-core` should:

1. Translate the Anthropic-format request body into OpenAI Chat
   Completions format.
2. Forward it to the upstream's `/chat/completions` endpoint.
3. Translate the response (streaming or non-streaming) back to Anthropic
   Messages format before returning it to the client.

Cost tracking, payment deduction, model rewriting, and provider
fallback must keep working unchanged.

## Approach: use `litellm` as the translation engine

After auditing the problem we determined the translator (request +
response + SSE event sequencing + tool_call buffering across deltas +
usage propagation + provider quirks like OpenAI's 64-char tool-name
limit) is non-trivial to maintain.

[`litellm`](https://docs.litellm.ai/docs/anthropic_unified) ships
exactly this capability as an in-process Python SDK call:

```python
import litellm
response = await litellm.anthropic.messages.acreate(
    model="openai/gpt-4o-mini",   # or groq/..., xai/..., gemini/..., etc.
    api_base="https://api.groq.com/openai/v1",
    api_key="...",
    messages=[...],
    max_tokens=1024,
    stream=True,
)
```

It accepts the Anthropic `/v1/messages` body, calls any LiteLLM-known
provider, and returns an Anthropic-shaped response (or an
`AsyncIterator` of Anthropic-shaped chunks for `stream=True`).

**No separate proxy process required.** litellm is just a Python library;
we `import litellm` and call the function in-process. The deployment
story is unchanged: still one `routstr-core` process.

## Constraints

- **Least changes / simplest design.** Reuse litellm; do not maintain our
  own translator.
- Must not break existing `/v1/messages` flow for Anthropic and
  OpenRouter (already working).
- Must respect `CLAUDE.md`: clean `uv run ruff check . --fix` and
  `uv run mypy .` after every edit; affected unit tests pass.

## Design

### 1. Dependency

Add `litellm` to `pyproject.toml` `[project.dependencies]`.

### 2. Provider opt-in: `supports_anthropic_messages`

On `BaseUpstreamProvider` add a class attribute, default `False`:

```python
supports_anthropic_messages: bool = False
```

Override on the two providers that natively serve `/v1/messages`:
- `routstr/upstream/anthropic.py` → `supports_anthropic_messages = True`
- `routstr/upstream/openrouter.py` → `supports_anthropic_messages = True`

### 3. Provider → litellm prefix mapping: `litellm_provider_prefix`

On `BaseUpstreamProvider` add a class attribute, default `"openai/"`
(safe default — most non-listed providers are OpenAI-compatible and can
be reached by passing `api_base`):

```python
litellm_provider_prefix: str = "openai/"
```

Per-provider overrides where litellm has a native provider:
- `groq.py` → `"groq/"`
- `xai.py` → `"xai/"`
- `fireworks.py` → `"fireworks_ai/"`
- `perplexity.py` → `"perplexity/"`
- `gemini.py` → `"gemini/"`
- `ollama.py` → `"ollama/"`
- `azure.py` → `"azure/"`
- `openai.py`, `ppqai.py`, `routstr.py`, `generic.py` → keep default `"openai/"`

litellm derives auth from `api_base` + `api_key` we pass, so
"openai/" + custom `api_base` works for any OpenAI-compatible upstream.

### 4. New helper on `BaseUpstreamProvider`: `_forward_messages_via_litellm`

Single private method that owns the full litellm path: build kwargs,
call `litellm.anthropic.messages.acreate`, run cost tracking, return
`Response` or `StreamingResponse`. Pure addition — does not touch
existing chat/completions or messages paths.

Signature:

```python
async def _forward_messages_via_litellm(
    self,
    request_body: bytes | None,
    key_or_payment: ApiKey | XCashuPaymentContext,
    session: AsyncSession | None,
    max_cost_for_model: int,
    model_obj: Model,
    *,
    request_id: str | None = None,
    is_x_cashu: bool = False,
    mint: str | None = None,
    payment_token_hash: str | None = None,
) -> Response | StreamingResponse:
    ...
```

Behaviour:
1. Parse `request_body` as JSON. Pop `model` (we override with our
   transformed id). Read `stream` flag.
2. Build litellm kwargs:
   - `model = f"{self.litellm_provider_prefix}{self.transform_model_name(model_obj.id)}"`
   - `api_key = self.api_key`
   - `api_base = self.base_url`
   - splat the rest of the body (messages, max_tokens, system, tools,
     tool_choice, temperature, top_p, top_k, stop_sequences, metadata,
     thinking, stream).
3. `result = await litellm.anthropic.messages.acreate(**kwargs)`.
4. Non-streaming branch (`stream=False`): `result` is an Anthropic
   `AnthropicMessagesResponse` dict-shaped object. Wrap into the same
   shape `handle_non_streaming_messages_completion` produces:
   - Run `adjust_payment_for_tokens(key, response_dict, session, max_cost_for_model)`
     for the bearer-key path, or compute x-cashu refund for the x-cashu
     path.
   - Call `inject_cost_metadata(response_dict, cost_data, key)` (bearer
     path) — for x-cashu, mirror `handle_x_cashu_chat_completion`'s
     `X-Cashu` refund header logic.
   - Return `Response(content=json.dumps(response_dict).encode(), media_type="application/json", ...)`.
5. Streaming branch (`stream=True`): `result` is an
   `AsyncIterator` of Anthropic event dicts. We:
   - Wrap it in an async generator that:
     - For each event, serialize as SSE
       (`event: <type>\ndata: <json>\n\n`).
     - Tap `message_start` for `usage.input_tokens`, accumulate
       `output_tokens` from `message_delta` `usage.output_tokens`.
     - On stream end, run cost reconciliation
       (`adjust_payment_for_tokens` for bearer; refund/lock for
       x-cashu) using the captured usage.
   - Return `StreamingResponse(generator(), media_type="text/event-stream")`.

Reusing `adjust_payment_for_tokens` and `inject_cost_metadata` keeps
cost calculation identical between native and translated paths — they
already operate on Anthropic Messages response shape.

### 5. Wire into `forward_request` (base.py:1403)

At the start of the `if path.endswith("messages"):` branch, before the
existing httpx call, add:

```python
if path.endswith("messages") and not self.supports_anthropic_messages:
    return await self._forward_messages_via_litellm(
        request_body=request_body,
        key_or_payment=key,
        session=session,
        max_cost_for_model=max_cost_for_model,
        model_obj=model_obj,
        request_id=getattr(request.state, "request_id", None),
    )
```

Critical predicate ordering: this branch must be reached **only** for
`/messages` (not `/messages/count_tokens`). The current code already
checks `messages/count_tokens` separately, but our shortcut goes
**before** the httpx call so we must explicitly exclude count_tokens
ourselves, e.g. `path.endswith("messages") and not path.endswith("count_tokens")`.

### 6. Wire into `forward_x_cashu_request` (base.py:2583)

Same shortcut at the same logical location. We pass
`is_x_cashu=True` plus `mint` and `payment_token_hash` so the helper
takes the x-cashu refund branch.

### 7. count_tokens is out of scope

`/messages/count_tokens` requests against non-supporting providers stay
unchanged — they get the existing 404 response from the upstream. Same
behaviour as today, no regression.

## Files touched

- **Edit**: `pyproject.toml` (add `litellm`)
- **Edit**: `routstr/upstream/base.py`
  - Add `supports_anthropic_messages: bool = False`
  - Add `litellm_provider_prefix: str = "openai/"`
  - Add `_forward_messages_via_litellm` method
  - Branch into it from `forward_request` and `forward_x_cashu_request`
- **Edit**: `routstr/upstream/anthropic.py` (1 line — flag)
- **Edit**: `routstr/upstream/openrouter.py` (1 line — flag)
- **Edit**: `routstr/upstream/{groq,xai,fireworks,perplexity,gemini,ollama,azure}.py`
  (1 line each — `litellm_provider_prefix`)
- **New**: `tests/unit/test_messages_litellm_dispatch.py`

No new translator module, no per-provider override of forward paths,
no SSE rewriting code — it all lives inside litellm.

## Tests

`tests/unit/test_messages_litellm_dispatch.py`:
- Bearer-key path: dispatches to litellm when `supports_anthropic_messages=False`,
  bypasses litellm when `True`.
- Non-streaming response: usage extracted, `adjust_payment_for_tokens`
  called, cost metadata injected.
- Streaming response: usage extracted from `message_delta` events,
  cost reconciled at end.
- x-cashu path: refund header populated.

We mock `litellm.anthropic.messages.acreate` so tests do not require
network or upstream API keys.

## Out of scope

- `/v1/messages/count_tokens` translation — left to upstream to 404 as
  today.
- Per-provider tuning of litellm options
  (e.g. `litellm.use_chat_completions_url_for_anthropic_messages`).
  Sane defaults; revisit only if a specific upstream misbehaves.

## Verification

After implementation, per `CLAUDE.md`:

```bash
uv run ruff check . --fix
uv run mypy .
uv run pytest tests/unit/test_messages_litellm_dispatch.py
uv run pytest tests/unit  # full unit suite still green
```

## Spike before merge

Before opening the PR, run a manual one-shot:

```python
import asyncio, litellm
async def main():
    r = await litellm.anthropic.messages.acreate(
        model="groq/llama-3.3-70b-versatile",
        api_base="https://api.groq.com/openai/v1",
        api_key=os.environ["GROQ_API_KEY"],
        messages=[{"role": "user", "content": "say hi"}],
        max_tokens=64,
        stream=True,
    )
    async for chunk in r:
        print(chunk)
asyncio.run(main())
```

Confirms streaming, usage propagation and tool-format on a real
OpenAI-compatible upstream before we trust it in production.
