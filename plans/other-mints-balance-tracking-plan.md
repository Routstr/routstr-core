# Plan: Track unsupported incoming mints in `other_mints` and include them in balances

## Goal

When a Cashu token arrives from a mint that is **not** in `settings.cashu_mints`, we currently create/use a wallet for that mint and swap value into the primary mint. Any change/surplus left behind after `melt()` remains in the foreign `token_wallet`, but that balance is not surfaced by `fetch_all_balances()` because it only iterates over configured mints.

This plan adds persistent tracking for those foreign mints in a new database table called `other_mints`, and updates balance reporting to include them.

---

## Current behavior

### Incoming unsupported mint flow

In `routstr/wallet.py`:

- `recieve_token()` deserializes the token
- if `token_obj.mint not in settings.cashu_mints`, it calls `swap_to_primary_mint(token_obj, wallet)`
- `swap_to_primary_mint()` calls `token_wallet.melt(...)` to pay the primary mint invoice

### Important detail: change is retained, not discarded

The underlying Cashu wallet library keeps any melt change:

- `Wallet.melt()` constructs blank outputs for change
- when the melt succeeds, returned change is reconstructed into proofs
- those proofs are appended to `self.proofs` and stored in the wallet DB

So surplus from unsupported mints is **not discarded**, but it may become invisible operationally.

### Visibility problem

`fetch_all_balances()` currently only loops over:

- `settings.cashu_mints`
- units `sat` and `msat`

This means balances left on unsupported mints are not shown in admin balance reporting.

---

## Proposed design

## 1. Add a new DB table: `other_mints`

Add a small table in `routstr/core/db.py` to persist unsupported mints we have seen in incoming tokens.

Suggested schema:

- `mint_url: str` primary key
- `created_at: int`
- `last_seen_at: int`

Minimal model:

```python
class OtherMint(SQLModel, table=True):
    __tablename__ = "other_mints"

    mint_url: str = Field(primary_key=True)
    created_at: int = Field(default_factory=lambda: int(time.time()))
    last_seen_at: int = Field(default_factory=lambda: int(time.time()))
```

Why minimal:

- the only required function is mint discovery/tracking
- unit handling can remain dynamic via existing balance queries over `sat` and `msat`

---

## 2. Add DB helpers for `other_mints`

In `routstr/core/db.py`, add helper functions:

### `register_other_mint(mint_url: str) -> None`

Behavior:

- if the mint is not present, insert it
- if it already exists, update `last_seen_at`

### `list_other_mints(session) -> list[str]`

Behavior:

- return all tracked unsupported mint URLs

Optional later:

- `delete_other_mint(...)`
- admin cleanup helpers

---

## 3. Register unsupported mints during token receipt

Update `recieve_token()` in `routstr/wallet.py`.

Current logic:

```python
if token_obj.mint not in settings.cashu_mints:
    return await swap_to_primary_mint(token_obj, wallet)
```

Planned logic:

```python
if token_obj.mint not in settings.cashu_mints:
    await db.register_other_mint(token_obj.mint)
    return await swap_to_primary_mint(token_obj, wallet)
```

Why here:

- this is the earliest reliable point where we know the mint came in via an actual token
- this is exactly the path that can leave foreign-mint change behind
- it avoids needing to infer unsupported mints later from wallet internals

---

## 4. Update `fetch_all_balances()` to include `other_mints`

Current behavior only includes configured mints.

Planned behavior:

- load tracked unsupported mints from DB
- combine them with `settings.cashu_mints`
- dedupe while preserving order
- fetch balances for all tracked mints across requested units

Conceptual flow:

```python
tracked_mints = dedupe(settings.cashu_mints + other_mints_from_db)
```

Then existing per-mint/per-unit balance logic can remain mostly unchanged.

This ensures that retained change on unsupported mints becomes visible in admin balance reporting.

---

## 5. Add a balance source marker

Extend `BalanceDetail` in `routstr/wallet.py` to identify whether a balance row comes from a configured mint or an `other_mints` entry.

Suggested field:

- `source: str` with values:
  - `"configured"`
  - `"other"`

Updated shape:

```python
class BalanceDetail(TypedDict, total=False):
    mint_url: str
    unit: str
    source: str
    wallet_balance: int
    user_balance: int
    owner_balance: int
    error: str
```

Why this helps:

- admin can distinguish normal configured wallet balances from foreign/unsupported balances
- avoids confusion if unexpected mint URLs show up in the balances API/UI

---

## 6. Admin/API impact

Backend impact is minimal because `/admin/api/balances` already returns `fetch_all_balances()` output.

Effects:

- supported mints continue to show as before
- tracked unsupported mints will also appear
- UI can optionally display the new `source` field

No API contract break is expected if the frontend ignores unknown fields.

---

## 7. Payout behavior: do not change in phase 1

`periodic_payout()` currently only iterates over `settings.cashu_mints`.

Recommendation for this change:

- **do not** expand `periodic_payout()` to include `other_mints` yet
- only improve visibility through balance reporting

Reason:

- automatic payout from unsupported/foreign mints may be operationally undesirable
- visibility should come first, automation second

Possible future phase:

- add optional sweeping/payout support for `other_mints`
- or provide an admin-triggered withdrawal/sweep flow

---

## 8. Logging improvements (optional)

Optional follow-up improvement in `swap_to_primary_mint()`:

- capture the return value from `token_wallet.melt(...)`
- if feasible, log any reported change amount
- otherwise, rely on wallet balance reporting to surface residual amounts

This is useful but not required for the first implementation.

---

## Files to change

### `routstr/core/db.py`

Add:

- `OtherMint` SQLModel
- `register_other_mint()`
- `list_other_mints()`

### `migrations/versions/<new_revision>_add_other_mints_table.py`

Create migration to add the `other_mints` table.

### `routstr/wallet.py`

Update:

- `recieve_token()` to register unsupported mints
- `BalanceDetail` to include `source`
- `fetch_all_balances()` to include both configured and tracked unsupported mints

### `routstr/core/admin.py`

Likely no backend changes required unless a dedicated `other_mints` API is desired.

---

## Behavior rules

### Register a mint when

- an incoming token is processed
- the token mint is not in `settings.cashu_mints`

### Do not remove automatically when

- balance reaches zero

Reason:

- historical visibility is useful
- avoids flapping entries in the admin balance list
- mint may receive additional unsupported tokens later

Potential future enhancement:

- admin endpoint to prune zero-balance `other_mints`

---

## Edge cases

### A mint later becomes configured

If a mint in `other_mints` is later added to `settings.cashu_mints`:

- deduplication prevents duplicate balance rows
- `source` should resolve to `configured`

### Unsupported mint with zero balance

A tracked unsupported mint may show zero balances.

Initial recommendation:

- allow it to appear
- consider later filtering zero-balance `other` rows if the UI becomes noisy

### Units

Balance fetching can continue to query both `sat` and `msat` for each tracked mint.

If a mint has no proofs in one unit, current error/zero handling can continue to apply.

---

## Test plan

### DB tests

- registering a new unsupported mint inserts a row
- registering the same mint again updates `last_seen_at` without duplication
- listing other mints returns expected mint URLs

### Wallet tests

#### `recieve_token()`

- when mint is unsupported, `db.register_other_mint()` is called before swap
- when mint is configured, `db.register_other_mint()` is not called

#### `fetch_all_balances()`

- includes configured mints
- includes `other_mints` from DB
- dedupes if a mint exists in both configured and other lists
- sets `source` correctly

### Regression tests

- existing trusted mint balance reporting remains unchanged
- `/admin/api/balances` continues to work

---

## Recommended implementation order

1. Add `OtherMint` model to `routstr/core/db.py`
2. Add Alembic migration for `other_mints`
3. Add `register_other_mint()` and `list_other_mints()` helpers
4. Update `recieve_token()` to register unsupported mints
5. Update `fetch_all_balances()` to union configured + tracked other mints
6. Add `source` to `BalanceDetail`
7. Add/adjust tests

---

## Summary

This change solves an operational visibility problem:

- unsupported incoming mints can leave retained change in foreign wallets
- those funds are currently preserved but not surfaced in balance reporting
- introducing `other_mints` makes those mints discoverable and auditable
- expanding `fetch_all_balances()` ensures their balances are visible in admin tooling

Recommended scope for the first pass:

- track unsupported mints in DB
- include them in balance reporting
- mark them as `source="other"`
- do not yet change payout/sweeping behavior
