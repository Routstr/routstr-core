# Standardizing the HTTP 402 Error Response Format

**Status:** Core additive implementation included in this branch; SDK tracked separately.
Section 3.4 remains a deferred, separate pricing behavior proposal.

### Implementation notes (supersede draft examples below)

- All five local payment 402 producers use the shared serializer (early auth
  through the retained `_model_balance_error` compatibility wrapper). Amounts
  are integers in `msat`; reservations are subtracted once by the serializer.
- The proxy captures discount inputs/deltas during the actual calculation,
  including image estimates, without re-running estimation or fetching images.
- Pricing uses `base_source`, `max_cost_formula`, and a `formula` alias. Fixed
  and fallback pricing use `formula: "fixed_request_cost"`. Direct auth checks
  receive only `min_cost`, not a model/body, and explicitly report `unavailable`;
  proxy-driven early rejection is enriched with its already computed model/trail.
- Cached sat pricing is authoritative. The model cache does not store historical
  FX; the implementation does not invent an exchange rate or imply today's rate
  reproduces the cached costs. `max_cost_computed` distinguishes current-input
  USD formula calculations from cached USD/sat ceilings. Fees already included
  in published model rates are not independently reconstructed.
- The concurrent depletion alias stays at its original `error.available_balance`
  path (not moved into `details`). Legacy X-Cashu keys stay in `detail`.
- The SDK sketch in section 6 is illustrative; consumers must preserve the
  actual pricing object, not require the older `inputs`/`computed` sketch.
- Focused producer/handler and formula tests live in `tests/unit/test_402_errors.py`.
  Section 9 remains the original cross-repository rollout checklist, not a claim
  that deferred pricing or SDK work shipped in this core branch.
**Owners:** routstr-core + routstr-sdk
**Related:** `routstr/core/exceptions.py`, `routstr/auth.py`, `routstr/payment/helpers.py`, `routstr/payment/models.py`, `routstr-sdk/core/errorTypes.ts`

---

## 0. TL;DR — key decisions

1. **One canonical envelope.** Every 402 body has the same top-level shape:
   `{"error": {...}, "detail": {"error": {...}}, "request_id": "..."}`.
   The top-level `error` is canonical; `detail.error` is kept as a byte-identical
   compatibility mirror **forever** (not deprecated away).
2. **Real JSON numbers, not English.** `error.details` carries
   `required_msats`, `available_msats`, `shortfall_msats`, `reserved_msats`,
   `model`, and `unit`.
3. **Pricing audit trail.** `error.details.pricing` exposes the **formula used
   to compute `required_msats`**, its named variant, the inputs
   (context length, token limits, USD prices, exchange rate), and the final
   computed values. Clients can display "why this number" without guessing.
4. **Discriminators always present.** `error.type` and `error.code` are always
   strings at a predictable path.
5. **Single serializer.** All five 402 producers route through one helper so
   shapes cannot drift.
6. **Backward compatible.** No key is removed or moved. The only additions are
   `error.details` (and its `pricing` sub-object) plus an `error` key at the one
   flat-shape producer. Legacy keys (`available_balance`, `amount_required_msat`,
   `reason`, flat `type`) are retained.
7. **Rollout is additive.** No major version, no breaking change, no version
   gate. SDK ships first and stays compatible with old nodes.

---

## 1. Problem statement

### 1.1 The duplication bug

`routstr/core/exceptions.py` → `http_exception_handler` currently does:

```python
if isinstance(detail, dict) and "error" in detail:
    content = {"detail": detail, **detail}   # <-- spreads "error" to top level
else:
    content = {"detail": detail}
content["request_id"] = request_id
```

A 402 raised as `{"error": {...}}` therefore serializes the **same object twice**:

```json
{
  "detail": {"error": {"message": "...", "type": "insufficient_quota", "code": "insufficient_balance"}},
  "error":  {"message": "...", "type": "insufficient_quota", "code": "insufficient_balance"},
  "request_id": "..."
}
```

The duplication was added deliberately in commit `63f14dbd` ("preserve error
field when exception thrown") to satisfy two client conventions at once:
FastAPI clients read `detail.error`; OpenAI-style clients read top-level
`error`. `docs/api/errors.md` documents this mirroring for the Lightning
endpoints. It is cosmetic redundancy, not a correctness bug — both copies come
from the same object, so they cannot drift. **The plan keeps it.**

### 1.2 The real problems

1. **Shape drift at the X-Cashu producer.** `routstr/payment/helpers.py:115`
   raises a flat object with no `error` key:
   ```json
   {"detail": {"reason": "Insufficient balance", "amount_required_msat": 28989,
               "model": "gpt-4o", "type": "minimum_balance_required"},
    "request_id": "..."}
   ```
   No top-level `error`, no `code`, no `message`, no `available_msats`.
2. **Numbers only in English.** Even on the well-shaped path,
   `_model_balance_error` embeds `required`/`available` only inside the
   human-readable `message` string. The SDK regexes it and, in the reported
   incident, mis-read the node's figure (`28.989`) because the cached value was
   `15.2208`.
3. **No visibility into the formula.** Clients cannot tell whether
   `required_msats` reflects a context-length ceiling, a fixed request cost, a
   provider fee, or the minimum-request floor. There is no way to audit or
   explain the number.

### 1.3 Client contract today

`routstr-sdk/core/errorTypes.ts` → `parseCoreError` handles four formats:
top-level `error`, `detail.error`, `detail` as a string, and `detail` as a flat
object. It extracts only `type`, `code`, `message`, and optional `details`.
It does not extract numeric amounts, so it cannot populate the SDK's existing
`InsufficientBalanceError` without regexing text.

---

## 2. Canonical envelope

### 2.1 The shape

```json
{
  "error": {
    "type": "insufficient_quota",
    "code": "insufficient_balance",
    "message": "Insufficient balance: 28.989 sats (28989 msats) required for this model; 21 sats (21000 msats) available.",
    "details": {
      "unit": "msat",
      "required_msats": 28989,
      "available_msats": 21000,
      "shortfall_msats": 7989,
      "reserved_msats": 4000,
      "model": "gpt-4o",
      "pricing": { "...": "see section 3" }
    }
  },
  "detail": {
    "error": { "...": "byte-identical mirror of the top-level error object" }
  },
  "request_id": "req-7f3a9c21"
}
```

### 2.2 Guarantees

- `error` is always a top-level object for structured 402s.
- `error.type`, `error.code`, `error.message` are always present strings.
- `error.details` is always present for 402s and always contains
  `unit`, `required_msats`, `available_msats`, `shortfall_msats`.
- `detail.error` is byte-identical to `error` (generated from the same object).
- `request_id` stays top-level and unchanged.

### 2.3 Decision on the duplicate copy

| Option | Action | Decision |
|---|---|---|
| Keep both forever | `error` canonical; `detail.error` mirror | **Chosen.** Zero breakage, two client conventions both keep working. |
| Simplify `detail` | `error` canonical; `detail` a string | Rejected — breaks FastAPI-style clients for a cosmetic win. |
| Drop top-level `error` | keep only `detail.error` | Rejected — breaks the X-Cashu path and contradicts `docs/api/errors.md`. |

The duplication costs a few hundred bytes on an error path. It is generated from
one source, so it cannot drift. Keeping it means no client has to migrate.

### 2.4 Handler change

Replace the spread so only `error` is hoisted (a no-op for sites 1–4, and it
stops site #5's flat keys from leaking to the top level):

```python
if isinstance(detail, dict) and "error" in detail:
    content = {"detail": detail, "error": detail["error"]}
else:
    content = {"detail": detail}
content["request_id"] = request_id
```

---

## 3. Machine-readable numeric fields and the pricing formula

### 3.1 Balance fields (`error.details`)

| Field | Type | Required | Description |
|---|---|---|---|
| `unit` | string | yes | `"msat"` |
| `required_msats` | integer | yes | Amount required for the request. |
| `available_msats` | integer | yes | Spendable balance, net of reservations. |
| `shortfall_msats` | integer | yes | `max(0, required_msats - available_msats)`. |
| `reserved_msats` | integer | when non-zero | Already reserved for in-flight requests. |
| `model` | string | when known | Model the requirement applies to. |

`available_msats` must always be computed as `max(0, balance - reserved)` by the
serializer, so the number cannot drift between producers.

### 3.2 Pricing trail (`error.details.pricing`)

**This is the new requirement: every 402 must expose how `required_msats` was
derived.**

`required_msats` is not a single formula. It is a three-stage pipeline in
`routstr/proxy.py:984-988`:

```python
required_msats = calculate_discounted_max_cost(
    await get_max_cost_for_model(model=model_id, session=session, model_obj=model_obj),
    request_body_dict,
    model_obj=model_obj,
    path=path,
)
```

1. **Base source** — `get_max_cost_for_model()` (`payment/helpers.py:125`)
   picks the ceiling: fixed pricing, the model's cached `sats_pricing.max_cost`,
   or a fallback fixed cost.
2. **Max-cost formula** — only when the base is `model_sats_pricing`, the
   ceiling came from `_calculate_usd_max_costs()` (`payment/models.py:540`).
   That function has six branches (below), not seven. `fixed_request_cost` is
   **not** one of them; it is a separate base source.
3. **Discounts** — `calculate_discounted_max_cost()` (`payment/helpers.py:191`)
   subtracts prompt and completion deltas from the base, then floors at
   `min_request_msat`.

#### Proposed `pricing` object

```json
"pricing": {
  "base_source": "model_sats_pricing",
  "base_max_cost_msats": 1086666,
  "max_cost_formula": "context_plus_completion",
  "max_cost_inputs": {
    "context_length": 128000,
    "max_completion_tokens": 4096,
    "prompt_price_usd_per_token": 0.000005,
    "completion_price_usd_per_token": 0.000015,
    "request_price_usd": 0.0,
    "image_price_usd": 0.0,
    "web_search_price_usd": 0.0,
    "internal_reasoning_price_usd": 0.0
  },
  "max_cost_computed": {
    "max_prompt_cost_usd": 0.64,
    "max_completion_cost_usd": 0.06144,
    "max_cost_usd": 0.64,
    "exchange_rate_sats_to_usd": 0.0000000006,
    "max_cost_msats": 1066666
  },
  "discounts": {
    "tolerance_percentage": 5,
    "prompt_tokens_estimated": 1200,
    "prompt_delta_msats": 0,
    "completion_cap_used": 4096,
    "completion_delta_msats": 0,
    "image_reservation_msats": 0
  },
  "required_msats": 28989,
  "min_request_msat": 1000,
  "min_request_applied": false
}
```

#### Stage 1 — `pricing.base_source`

| `base_source` | Condition | Base amount |
|---|---|---|
| `fixed_pricing` | `settings.fixed_pricing` is on | `max(min_request_msat, fixed_cost_per_request * 1000)` |
| `model_sats_pricing` | `model_obj.sats_pricing` exists | `max(min_request_msat, int(sats_pricing.max_cost * 1000 * (1 - tolerance/100)))` |
| `fallback_fixed` | model not found, or no `sats_pricing` | `max(min_request_msat, fixed_cost_per_request * 1000)` |

#### Stage 2 — `pricing.max_cost_formula`

Only set when `base_source == "model_sats_pricing"`. The six branches of
`_calculate_usd_max_costs()`, which read `top_provider` first and fall back to
`model.context_length`:

| `max_cost_formula` | Condition | USD max cost |
|---|---|---|
| `context_plus_completion` | both limits exist, `context_length > max_completion_tokens` | `(cl - mct) * prompt_price + mct * completion_price` |
| `context_le_completion` | both limits exist, `context_length <= max_completion_tokens` | `cl * max(completion_price, prompt_price)` |
| `context_only` | `top_provider.context_length` only | `cl * max(completion_price, prompt_price)` |
| `completion_only` | `top_provider.max_completion_tokens` only | `mct * completion_price` |
| `model_context_only` | no `top_provider` limits; `model.context_length` exists | `model.context_length * max(completion_price, prompt_price)` |
| `flat_estimate` | no limits anywhere (`top_provider` absent/empty and `context_length` falsy) | `max(prompt*1_000_000 + completion*32_000 + request*100_000 + image*100 + web_search*1000 + internal_reasoning*100, min_req_usd)` |

Notes on reachability (verified against the upstream classes):

- `completion_only` is real but unusual: `venice.py` and `model_paths.py` set
  `context_length` and `max_completion_tokens` independently, so a provider can
  publish an output cap without a context window.
- `model_context_only` is the **common** path, not a rare fallback. The native
  provider classes (`openai.py`, `anthropic.py`, `perplexity.py`, `xai.py`,
  `tinfoil.py`, `ppqai.py`, `routstr.py`, `deepseek.py`) do not set
  `top_provider` at all and rely on `model.context_length`. `top_provider` is
  set by `generic.py`, `typesafe.py`, `venice.py`, `together.py`, `ollama.py`
  and `model_paths.py` — it is a refinement, not an OpenRouter-only field.
- `flat_estimate` is real: it is reached by any provider that declares neither
  a `top_provider` limit nor a nonzero `context_length`. The `request * 100_000`
  term makes this ceiling very large for a model with a nonzero per-request
  price, which is worth surfacing so operators can see why the reservation is
  unexpectedly high.

#### Stage 3 — `pricing.discounts`

`calculate_discounted_max_cost()` subtracts, in order:

- If the request carries images, the image reservation replaces the base
  entirely and no discount math runs.
- `prompt_delta_msats = floor((max_prompt_allowed_sats - prompt_tokens_estimated * sats_pricing.prompt) * 1000)`
  when positive, where `max_prompt_allowed_sats` uses the same prompt token
  limit as the formula above multiplied by `tolerance_percentage`.
- `completion_delta_msats` is the same computation for the largest of
  `max_tokens`, `max_completion_tokens`, and `max_output_tokens` against
  `max_completion_allowed_sats`.
- Final `required_msats = max(min_request_msat, adjusted)`.

When `settings.fixed_pricing` is on, `calculate_discounted_max_cost()` returns
its input unchanged — no discounts run. The `pricing` object must say so via
`base_source: "fixed_pricing"` and an empty `discounts` object.

#### Notes

- The `sats_pricing` object is already computed and cached on the model; the
  402 serializer must read it rather than recompute, so the reported number
  matches what was actually reserved.
- If the model has no usable pricing (`has_usable_pricing` false), emit
  `pricing.base_source: "unavailable"` and `pricing.error: "<reason>"` rather
  than failing the 402.
- When `prompt_price` and `completion_price` are both zero and there is no
  context data, `flat_estimate` collapses to `min_req_usd`, so the minimum
  request floor is what gets reserved. `min_request_applied` makes this
  visible.

### 3.3 Why expose the formula

- The reported incident showed the node requiring `28.989` while the SDK cached
  `15.2208`. Without the formula and inputs, neither side can explain the gap.
- Clients can show "128k context at $5/M prompt" instead of an opaque number.
- Node operators can debug why a model's reservation is unexpectedly high.
- It makes the minimum-request floor visible, which is a common surprise when a
  cheap model still demands a fixed amount.

### 3.4 Separate proposal: clamp `max_completion_tokens` to `context_length`

This is a **pricing behavior change**, not part of the additive 402 work. It is
recorded here because it determines what `required_msats` reports in the
`context_le_completion` case. It should ship as its own PR with its own
changelog line.

#### Motivation

When `top_provider.context_length <= top_provider.max_completion_tokens`,
`_calculate_usd_max_costs()` returns `cl * max(prompt_price, completion_price)`
as `max_cost`. The rest of the pipeline does not share that assumption:

- `calculate_discounted_max_cost()` sets
  `prompt_token_limit = max(0, cl - mct)`, which is `0` here, so no prompt
  discount can run.
- The completion discount is computed against
  `max_completion_cost = cl * completion_price`.

So when `prompt_price > completion_price` (rare, but legal), the reservation is
`cl * prompt_price` while the only ceiling the discount stage can discount is
`cl * completion_price`. The base is over-reserved relative to the ceiling that
actually gets adjusted.

#### Proposed change

Clamp `mct` to `cl` and always use the `context_plus_completion` formula:

```python
mct = min(mct, cl)
return (
    cl * prompt_price,
    mct * completion_price,
    (cl - mct) * prompt_price + mct * completion_price,
)
```

When `mct >= cl` this yields `max_cost = cl * completion_price`, and the
`max(...)` branch disappears. `max_prompt_cost` and `max_completion_cost` are
unchanged (`cl * prompt_price`, `cl * completion_price`).

#### Effect on `required_msats`

The chain is unchanged:

```
max_cost_usd
  -> / sats_to_usd
  -> * 1000
  -> * (1 - tolerance/100)
  -> minus prompt/completion deltas
  -> max(min_request_msat, adjusted)
```

Only `max_cost` differs, and only when `prompt_price > completion_price`:

| | Current | Clamped |
|---|---|---|
| `max_cost_usd` (completion > prompt) | `cl * completion` | `cl * completion` |
| `required_msats` (completion > prompt) | unchanged | **unchanged** |
| `max_cost_usd` (prompt > completion) | `cl * prompt` | `cl * completion` |
| `required_msats` (prompt > completion) | higher | **lower** |

Worked example with `prompt = $0.00002/token`, `completion = $0.00001/token`,
`cl = 4096`, `sats_to_usd = 0.0006`, `tolerance = 5%`:

| | Current | Clamped |
|---|---|---|
| `max_cost_usd` | `0.08192` | `0.04096` |
| `sats_pricing.max_cost` | `136.53` sats | `68.27` sats |
| `base_msats` | `129,706` | `64,853` |
| `required_msats` | `129,706` | `64,853` |

#### Impact

Changing `sats_pricing.max_cost` changes more than the 402 body:

- the 402 `required_msats` and `shortfall_msats`;
- the advertised model max cost;
- the actual reservation charged by `pay_for_request`;
- `calculate_model_reservation_score()` in `algorithm.py`, which calls
  `_calculate_usd_max_costs()` to rank same-model provider candidates.

Because routing scores and reservations move together, the change is internally
consistent — but it is a real pricing change and must not be smuggled into the
402 formatting PR.

#### Tests

- `completion_price > prompt_price` (the common case): assert `max_cost` and
  `required_msats` are **byte-identical** before and after the clamp.
- `prompt_price > completion_price`: assert `max_cost` drops from
  `cl * prompt_price` to `cl * completion_price` and `required_msats` follows.
- `mct < cl`: assert the `context_plus_completion` split is unchanged
  (`(cl - mct) * prompt + mct * completion`).
- `mct == cl`: assert `max_cost == cl * completion_price`.
- Assert `max_prompt_cost` and `max_completion_cost` are unchanged in every case.
- A routing test asserting `calculate_model_reservation_score()` tracks the new
  `max_cost`.

---

## 4. Discriminator fields

### 4.1 Placement

Always at `error.type` and `error.code`. No `detail.type`, no top-level
`type`/`code` without an enclosing `error`.

### 4.2 402 `type` values

| `type` | Meaning |
|---|---|
| `insufficient_quota` | Balance below the required amount (API-key path and X-Cashu path). |
| `minimum_balance_required` | Legacy flat type kept for compatibility; canonical `type` is still `insufficient_quota`. |

### 4.3 402 `code` values

| `code` | Meaning |
|---|---|
| `insufficient_balance` | Balance shortfall on the API-key path. |
| `minimum_balance_required` | X-Cashu token below the model's minimum. |

Redemption-adjacent codes listed for completeness but **not** currently 402:
`cashu_token_already_spent`, `cashu_mint_unreachable`,
`cashu_mint_rate_limited`, `cashu_token_swap_fees_exceed_amount`,
`cashu_foreign_mint_swap_failed`, `internal_error`.

### 4.4 Why always present

The SDK branches on `type`/`code` to choose failover vs. top-up vs. retry.
Missing discriminators force string matching. Every 402 serializer call must pass
both explicitly; no defaults inferred from HTTP status.

---

## 5. Consistency across producers

### 5.1 The five 402 raise sites

| # | Location | Current shape |
|---|---|---|
| 1 | `auth.py:221` `_validate_existing_cashu_key` | `_model_balance_error(min_cost, total_balance)` |
| 2 | `auth.py:350` `validate_bearer_key` | `_model_balance_error(min_cost, total_balance)` |
| 3 | `auth.py:612` `pay_for_request` pre-reservation | inline `{"error": {...}}` |
| 4 | `auth.py:704` `pay_for_request` concurrent depletion | inline `{"error": {..., "available_balance": N}}` |
| 5 | `payment/helpers.py:115` X-Cashu token shortfall | flat `{"reason", "amount_required_msat", "model", "type"}` |

### 5.2 Single serializer

Add to `routstr/core/errors.py` (new module) or extend
`routstr/payment/helpers.py`:

```python
def build_payment_required_error(
    *,
    error_type: str = "insufficient_quota",
    code: str,
    message: str,
    required_msats: int,
    balance_msats: int,
    reserved_msats: int = 0,
    model: str | None = None,
    pricing: dict[str, object] | None = None,
    unit: str = "msat",
    extra_details: dict[str, object] | None = None,
) -> dict[str, object]:
    available_msats = max(0, balance_msats - reserved_msats)
    details: dict[str, object] = {
        "unit": unit,
        "required_msats": required_msats,
        "available_msats": available_msats,
        "shortfall_msats": max(0, required_msats - available_msats),
    }
    if reserved_msats:
        details["reserved_msats"] = reserved_msats
    if model is not None:
        details["model"] = model
    if pricing is not None:
        details["pricing"] = pricing
    if extra_details:
        details.update(extra_details)
    return {
        "error": {
            "type": error_type,
            "code": code,
            "message": message,
            "details": details,
        }
    }
```

And a companion builder for the pricing trail:

```python
def build_pricing_trail(
    model: "Model",
    sats_to_usd: float,
    *,
    min_request_msat: int,
) -> dict[str, object]:
    """Return the formula variant, inputs, and computed costs for a 402."""
    # Read model.sats_pricing when present; fall back to recomputing via
    # _calculate_usd_max_costs(model).
    ...
```

### 5.3 Site-by-site migration

| # | Change |
|---|---|
| 1 | Replace `_model_balance_error(min_cost, total_balance)` with `build_payment_required_error(code="insufficient_balance", required_msats=min_cost, balance_msats=existing_key.balance, reserved_msats=existing_key.reserved_balance, model=model.id, pricing=build_pricing_trail(model, ...))`. |
| 2 | Same as #1 in `validate_bearer_key`. |
| 3 | Replace the inline dict; pass `billing_key.balance` / `billing_key.reserved_balance` and the pricing trail. |
| 4 | Replace the inline dict; reuse the already-fetched `total_balance` / `reserved_balance`. Keep `available_balance` as a flat alias in `extra_details` for compatibility. |
| 5 | Keep the flat keys in `detail`, but add a canonical `error` object. Set `code="minimum_balance_required"`, keep flat `type="minimum_balance_required"`, and add `details` + `pricing`. |

### 5.4 Deprecate `_model_balance_error`

Keep it as a thin wrapper for one release so downstream imports do not break:

```python
def _model_balance_error(required: int, available: int, **kwargs) -> dict[str, object]:
    return build_payment_required_error(
        code="insufficient_balance",
        required_msats=required,
        balance_msats=available,
        message=...,
        **kwargs,
    )
```

Remove once callers are migrated.

### 5.5 Non-402 structured errors

The same envelope (`error` top-level with `type`/`code`/`message`/`details`) is
the target for all structured errors. The handler already treats any `detail`
containing `error` the same way, and `create_error_response` in
`routstr/payment/helpers.py` already emits top-level `error`. Keep that helper
and make the handler match its shape.

---

## 6. Client parsing contract (routstr-sdk)

### 6.1 Updated `ParsedCoreError`

```ts
export interface ParsedCoreError {
  type?: string;
  code?: string;
  message?: string;
  details?: Record<string, unknown>;

  // Canonical 402 numerics
  requiredMsats?: number;
  availableMsats?: number;
  shortfallMsats?: number;
  reservedMsats?: number;
  model?: string;
  unit?: string;

  // Pricing trail
  pricing?: {
    formula: string;
    exchangeRateSatsToUsd?: number;
    inputs?: Record<string, unknown>;
    computed?: Record<string, unknown>;
  };

  status?: number;
  requestId?: string;
  raw: boolean;
  json?: boolean;
}
```

### 6.2 Extraction logic

1. Prefer top-level `error`; fall back to `detail.error`.
2. Copy numeric fields from `error.details` when their types match:
   ```ts
   const d = err.details as Record<string, unknown>;
   if (typeof d.required_msats === "number") result.requiredMsats = d.required_msats;
   if (typeof d.available_msats === "number") result.availableMsats = d.available_msats;
   if (typeof d.shortfall_msats === "number") result.shortfallMsats = d.shortfall_msats;
   if (typeof d.reserved_msats === "number") result.reservedMsats = d.reserved_msats;
   if (typeof d.model === "string") result.model = d.model;
   if (typeof d.unit === "string") result.unit = d.unit;
   if (d.pricing && typeof d.pricing === "object") result.pricing = d.pricing;
   ```
3. Keep Formats 3 and 4 for plain-string and flat `detail`.
4. Add a legacy branch for the flat `minimum_balance_required` shape:
   ```ts
   if (det.type === "minimum_balance_required") {
     result.code ??= "minimum_balance_required";
     if (typeof det.amount_required_msat === "number") {
       result.requiredMsats = det.amount_required_msat;
     }
     if (typeof det.model === "string") result.model = det.model;
     if (typeof det.reason === "string") result.message ??= det.reason;
   }
   ```
5. Keep the message-regex fallback for old nodes, but mark it deprecated with a
   TODO and remove it after the core breaking release.

### 6.3 Typed 402 result and guard

```ts
export interface PaymentRequiredError extends ParsedCoreError {
  type: "insufficient_quota" | "minimum_balance_required";
  code: "insufficient_balance" | "minimum_balance_required";
  requiredMsats: number;
  availableMsats: number;
  shortfallMsats: number;
  unit: "msat";
}

export function isPaymentRequiredError(
  e: ParsedCoreError
): e is PaymentRequiredError {
  return (
    e.status === 402 &&
    typeof e.requiredMsats === "number" &&
    typeof e.availableMsats === "number" &&
    typeof e.shortfallMsats === "number"
  );
}
```

### 6.4 Mapping to the existing `InsufficientBalanceError`

`core/errors.ts` already defines:

```ts
export class InsufficientBalanceError extends Error {
  constructor(
    public required: number,
    public available: number,
    public maxMintBalance: number = 0,
    public maxMintUrl: string = "",
    customMessage?: string
  ) { ... }
}
```

Call sites in `wallet/BalanceManager.ts` (lines 526, 1200, 1356) currently read
only `parsedError.message`. Add a mapping helper so they populate
`InsufficientBalanceError` from `requiredMsats`/`availableMsats` without
regexing. `routeRequests.ts:268` already preserves the typed error, so nothing
changes there.

### 6.5 Backward-compat matrix

| Node version | Envelope | SDK behavior |
|---|---|---|
| Pre-change | `detail.error` + top-level `error`, no `details` | Format 1/2; regex fallback for numerics |
| Additive change | Both copies with `details` and `pricing` | Format 1/2; read `details` and `pricing` |
| X-Cashu (new) | Top-level `error` + flat keys in `detail` | Format 1; read `details` |
| X-Cashu (old) | Flat `detail` only | Legacy branch; read `amount_required_msat` |

No SDK break in any case. The regex fallback is the only piece to retire later.

---

## 7. Tests

### 7.1 Core tests

**Serializer unit test** (`tests/unit/test_402_errors.py`):

```python
def test_build_payment_required_error_shape():
    payload = build_payment_required_error(
        code="insufficient_balance",
        message="Insufficient balance",
        required_msats=28989,
        balance_msats=25000,
        reserved_msats=4000,
        model="gpt-4o",
        pricing={"formula": "context_only", "computed": {"max_cost_msats": 28989}},
    )
    err = payload["error"]
    assert err["type"] == "insufficient_quota"
    assert err["code"] == "insufficient_balance"
    assert err["details"]["unit"] == "msat"
    assert err["details"]["required_msats"] == 28989
    assert err["details"]["available_msats"] == 21000      # 25000 - 4000
    assert err["details"]["shortfall_msats"] == 7989       # 28989 - 21000
    assert err["details"]["reserved_msats"] == 4000
    assert err["details"]["pricing"]["formula"] == "context_only"
    assert isinstance(err["details"]["required_msats"], int)
```

**Pricing trail tests:**

- One test per `formula` variant (`context_plus_completion`,
  `context_le_completion`, `context_only`, `completion_only`,
  `model_context_only`, `flat_estimate`, `fixed_request_cost`) asserting the
  reported `inputs` and `computed.max_cost_msats` match
  `_calculate_usd_max_costs()` and `_update_model_sats_pricing()`.
- A test that `min_request_applied` is `true` when the computed cost is below
  `min_request_msat`.
- A test that the `flat_estimate` coefficients are reported verbatim
  (`1_000_000`, `32_000`, `100_000`, `100`, `1000`, `100`).
- A test that `pricing.formula == "unavailable"` when
  `has_usable_pricing(model)` is false.

**Handler tests** (`tests/unit/test_core_exceptions.py`):

- Given a 402 detail, assert `detail.error` and top-level `error` are equal and
  both contain `details`.
- Assert `request_id` is present.
- Update the existing
  `test_structured_http_error_uses_standard_error_envelope` to assert the
  duplicate is intentional and identical, not accidental.

**Producer integration tests:**

- `tests/integration/test_insufficient_balance.py`
  `test_pay_for_request_raises_402_when_balance_too_low`:
  - `detail["error"]["details"]["required_msats"] == model_cost`
  - `detail["error"]["details"]["available_msats"] == user_balance`
  - `detail["error"]["details"]["shortfall_msats"] == model_cost - user_balance`
  - `detail["error"]["details"]["pricing"]["formula"]` is a known variant
- Add equivalent assertions for the zero-balance and all-reserved cases.
- Add a test for site #1 (`_validate_existing_cashu_key`).
- Add a test for site #2 (`validate_bearer_key`).
- Add a test for site #5 (X-Cashu `minimum_balance_required`):
  - `error.type == "insufficient_quota"`
  - `error.code == "minimum_balance_required"`
  - flat `detail.type == "minimum_balance_required"` still present
  - flat `detail.amount_required_msat` and `detail.reason` still present
  - `error.details.pricing.formula` present

**Shape regression test:**

- A parametrized test over all five producers asserting the body matches a JSON
  schema with `error.type`, `error.code`, `error.message`,
  `error.details.required_msats`, `error.details.available_msats`,
  `error.details.shortfall_msats`, `error.details.unit`,
  `error.details.pricing.formula`, and `request_id`.

### 7.2 SDK tests

**Parser tests** (`tests/unit/paymentRequiredError.test.ts`):

1. Canonical 402 body → `requiredMsats`, `availableMsats`, `shortfallMsats`,
   `model`, `unit`, and `pricing.formula` populated.
2. Duplicated body (`detail.error` + top-level `error`) → same numerics from
   either copy.
3. Flat `minimum_balance_required` legacy body → `requiredMsats` from
   `amount_required_msat`, `model`, and `code` populated.
4. **No-regex guarantee:** a body whose `message` says `"15.2208"` but whose
   `details.required_msats` is `28989` must return `28989`. This encodes the
   incident fix.
5. `pricing` is surfaced as an object when present and `undefined` when absent.
6. `isPaymentRequiredError` returns `true` only for 402 with all three numeric
   fields; `false` for 402 without them and for non-402 statuses.
7. Old bodies without `details` still parse via the regex fallback (deprecated
   path).

**Integration test:**

- Mock a core 402 response and assert the SDK surfaces `requiredMsats` without
  calling the regex helper. Assert the parsed value matches the server figure
  when the message and `details` disagree. Assert `pricing.formula` is exposed
  to the caller.

---

## 8. Rollout

### 8.1 Order of operations

1. **SDK first (minor release).** Add numeric + pricing extraction to
   `parseCoreError`, add `PaymentRequiredError` guard, and map to
   `InsufficientBalanceError` in `wallet/BalanceManager.ts`. Reads `details`
   when present; falls back to flat shape and regex for old nodes. No break.
2. **Core additive (minor release).** Add `build_payment_required_error` and
   `build_pricing_trail`; migrate sites #1–#5. Keep the duplicate `detail.error`
   copy. Update handler tests to assert both copies carry `details`.
3. **Docs.** Update `docs/api/errors.md` with the canonical 402 example, the
   `details` field table, and the `pricing` formula table. Update
   `docs/client/integration.md` to read `details.required_msats` and
   `details.pricing.formula`. Add a `CHANGELOG.md` entry.
4. **Pricing simplification (separate PR, independent).** Clamp `mct` to `cl`
   in `_calculate_usd_max_costs()` (see 3.4). This is a pricing change, not a
   402-formatting change; it can ship before, after, or without the rest, but it
   must carry its own tests and changelog line.
5. **Cleanup (optional, later).** Remove the message-regex fallback from the SDK
   once old nodes are rare. This is a soft deprecation, not a breaking release.

### 8.2 No breaking release

Because every change is additive and no key is removed or moved, there is no
major version, no `X-Routstr-Error-Format` header, and no deprecation window.
The duplicate `detail.error` copy stays permanently as a documented alias.

### 8.3 Docs updates

- `docs/api/errors.md`:
  - Add a "402 Payment Required" section with the canonical envelope, the
    `details` field table, and the `pricing` formula table.
  - State explicitly that top-level `error` is canonical and `detail.error` is a
    compatibility mirror.
  - Correct the existing "Insufficient Balance" example (it currently shows
    `type: "insufficient_balance"` and `code: "payment_required"`, which do not
    match the code).
- `docs/client/integration.md`: show reading `error.details.required_msats`,
  `available_msats`, `shortfall_msats`, and `error.details.pricing.formula`.
- `CHANGELOG.md`: additive `details` and `pricing`.
- `routstr-sdk/core/errorTypes.ts`: update the header comment and extend
  `ParsedCoreError`.

### 8.4 Open questions for implementation review

1. **Unit string:** `"msat"` (recommended) vs `"msats"`. Pick one and use it in
   both core and SDK.
2. **`shortfall_msats`:** compute server-side (recommended) or leave to clients?
   Server-side avoids client-side drift.
3. **Pricing trail verbosity:** always include `inputs`, or only when the client
   asks (e.g. via a query flag)? Recommend always, since 402s are rare.
4. **Provider fee:** should the pricing trail include the Routstr platform fee
   (`ROUTSTR_FEE_PERCENT`) when it applies? Recommend yes, as a named field in
   `computed`.
5. **Redemption codes as 402:** keep `token_already_spent`/`mint_error` at their
   current 400/422 statuses. Only list them in the enum for completeness.
6. **Site #4 alias:** keep flat `available_balance` in `details` for
   compatibility (recommended) or remove it? Recommend keep.
7. **`_model_balance_error` removal:** keep the wrapper for one release or
   migrate all callers at once? Recommend keep for one release.

---

## 9. Acceptance criteria

- [ ] Every 402 response contains `error.type`, `error.code`, `error.message`,
      `error.details.required_msats`, `error.details.available_msats`,
      `error.details.shortfall_msats`, `error.details.unit`, and
      `error.details.pricing.formula`.
- [ ] `error.details.pricing` reports the formula variant, inputs, and computed
      values for every 402.
- [ ] All five 402 producers call `build_payment_required_error`; no inline 402
      dicts remain.
- [ ] `http_exception_handler` no longer spreads `detail` into the top level
      (`{**detail}` is gone).
- [ ] Core tests assert the exact JSON shape for each producer, the handler, and
      each pricing formula variant.
- [ ] SDK `parseCoreError` returns numeric fields and `pricing` without regexing
      the message.
- [ ] A test proves the SDK uses `details` numbers when the message disagrees.
- [ ] `docs/api/errors.md` documents the canonical 402 envelope, the `details`
      table, the `pricing` formula table, and the permanent `detail.error`
      alias.

### 9.1 Separate pricing-simplification criteria (own PR)

- [ ] `_calculate_usd_max_costs()` clamps `mct = min(mct, cl)` and uses the
      `context_plus_completion` formula for every case where both limits exist.
- [ ] `completion_price > prompt_price` produces byte-identical `max_cost` and
      `required_msats` before and after the clamp.
- [ ] `prompt_price > completion_price` produces the lower clamped `max_cost`
      and `required_msats`.
- [ ] `max_prompt_cost` and `max_completion_cost` are unchanged in every case.
- [ ] `calculate_model_reservation_score()` tracks the new `max_cost`.
- [ ] The PR has its own changelog line and does not ride along with the 402
      formatting changes.
