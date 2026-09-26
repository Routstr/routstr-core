# Streaming latency patterns

Patterns taken from LiteLLM 1.93 (`litellm/proxy/pass_through_endpoints/`,
`litellm/proxy/common_request_processing.py`, `litellm/litellm_core_utils/logging_worker.py`)
and how they map onto Routstr's streaming hot path in `routstr/upstream/base.py`.

## Where time goes today

Every SSE event in `handle_streaming_chat_completion` and
`handle_streaming_responses_completion` is parsed, mutated (`model`, `id`,
`provider`, `provider_url`), observed for usage, and reserialized. Cost scales with
chunks per second times concurrent streams, all on one event-loop thread.

## 1. Fast JSON in the per-chunk path — done

LiteLLM parses request bodies with `orjson` (`common_utils/http_parsing_utils.py`).

Routstr: `routstr/upstream/json_codec.py` wraps `orjson` with a stdlib fallback
(orjson rejects `NaN` on load and non-string keys / >64-bit ints on dump). Used for
the per-event parse and reserialize in both streaming paths.

Wire change: emitted events are compact UTF-8 JSON (`{"a":1}`, raw `é`) instead of
stdlib's `{"a": 1}` with `\u00e9`. Both are valid JSON.

Measured on a typical chat chunk: 2.85µs → 0.54µs per parse+serialize (5.3x).

## 2. Resolve per-stream invariants once — done (partial)

LiteLLM computes `fast_path`, `cost_injection_active` and `debug_enabled` once per
stream, then runs a branch-free loop (`common_request_processing.py:2632`).

Routstr: `_apply_provider_field` (and the OpenRouter/generic overrides) ran
`public_provider_url(self.base_url)` — a `urlsplit` plus `ipaddress` parse — on every
chunk. It is now `lru_cache`d in `routstr/upstream/model_paths.py`, which keeps
subclass semantics. 1.67µs → 0.03µs per chunk.

Combined per-chunk saving from 1 and 2: 4.52µs → 0.57µs.

## 3. Linear buffer handling — next

`buffer = (buffer + chunk).replace(b"\r\n", b"\n")` re-copies and rescans the whole
unconsumed buffer on every network chunk, and `b"\n\n" in buffer` rescans it again.
This is quadratic in event size — it bites on large single events such as
Responses API `response.completed`, which carries the full output. Normalize only the
new chunk (holding back a trailing `\r`) and search for the delimiter from the
previous end offset.

## 4. Raw passthrough, parse usage at end — next

LiteLLM's pass-through hot path forwards `aiter_bytes()` chunks untouched and appends
them to `raw_bytes`; usage is reconstructed once after the stream
(`streaming_handler.py:chunk_processor`, `_convert_raw_bytes_to_str_lines`).

Routstr parses every event to rewrite `model`/`id` and feed
`MissingUsageEstimator.observe`. Candidates to skip parsing: events whose `model`
already equals `requested_model` and whose `id` is stable, with usage observed from a
cheap byte check (`b'"usage"'`) or at end of stream. LiteLLM makes byte-level mutation
safe by returning the original chunk on any failure
(`_process_chunk_with_cost_injection`). Needs a framing test suite before starting.

## 5. Settlement off the response path — next

LiteLLM enqueues end-of-stream work on a bounded `asyncio.Queue` with a semaphore
and per-task timeout (`logging_worker.py`, "+200 RPS").

Routstr runs `adjust_payment_for_tokens` inline after the last upstream chunk, so the
client waits on a DB session and writes before the stream closes. The reservation is
already persisted, so settlement can move to a worker and the stale-reservation sweep
stays the backstop. Unlike LiteLLM's logging queue, this queue must never drop work,
and the cost trailer the client receives must be computed before the stream closes or
be dropped from the contract.

## Related, outside the streaming loop

- `keys.db` runs WAL with default `synchronous=FULL`, so every reservation commit
  fsyncs before the upstream request is sent. `synchronous=NORMAL` removes that and
  cannot corrupt the database.
- `fastapi run` serves one worker. Multiple workers are blocked: the lifespan starts
  payout, auto top-up and refund tasks per process, and there is no leader election.
- There is no inbound admission control. A concurrency gate that returns 429 with
  `Retry-After` before `pay_for_request` would shed load before it reaches the DB.
