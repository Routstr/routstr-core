# TypeSafe `/v1/systemone` support — changes & test report

**Repo:** `~/projects/routstr-core`  **Branch:** `feat/systemone-typesafe` (cut from `origin/main` @ `c1b610d4`)
**Date:** 2026-09-19  **Status:** code complete, tests green, **not deployed** (no container restart)

---

## 1. What was added

Support for TypeSafe's System One decision endpoint — `POST /v1/systemone`
(`{state, model, questions}` → `{model, answers, usage}`) — as a first-class
Routstr endpoint with a dedicated upstream provider.

| File | Change |
|---|---|
| `routstr/upstream/typesafe.py` | **new** — `TypeSafeUpstreamProvider`: fixed base URL `https://api.typesafe.ai/v1`, `fetch_models()` maps TypeSafe's pricing-less `GET /v1/models` onto priced `Model` objects ($0.042/M input, $0 output, 64k context, `text->decisions`), catalog failures fail closed |
| `routstr/upstream/__init__.py` | provider registered → appears in the admin UI provider-type dropdown |
| `routstr/proxy.py` | `systemone` added to `_ALLOWED_ENDPOINTS` (POST only) |
| `routstr/upstream/base.py` | **two settlement fixes** (see below) |
| `routstr/upstream/helpers.py` | `TYPESAFE_API_KEY` env seeding (empty provider table only) |
| `docs/api/overview.md`, `docs/api/endpoints.md` | endpoint documented |

### The two `base.py` fixes matter most

1. `_x_cashu_path_has_settlement_handler` — now admits `systemone`, so ecash
   payments settle and refund the delta instead of being rejected with
   `x_cashu_unsupported_endpoint`.
2. `forward_request`'s response-settlement branch — now includes
   `systemone`. **Without this the endpoint served free**: the request fell
   through to the generic streaming path, whose `_finalize_generic_streaming_payment`
   releases the reservation *without charging* (usage never read).

---

## 2. Test results

Both runs executed against the working tree on the branch above.

### Targeted suites — 16/16 pass

```
pytest tests/unit/test_typesafe_integration.py tests/integration/test_systemone.py
→ 16 passed
```

Covers: allowlist admit/refuse (incl. `systemonedump`, `v1/systemone/secret`,
traversal spellings, GET/DELETE), x-cashu gate, provider metadata,
`fetch_models` pricing + error handling, and an end-to-end proxied request that
asserts the upstream hop is exactly `https://api.typesafe.ai/v1/systemone` and
that billing settles from usage (1000 input × 0.001 sats = 1000 msats, output
free) — plus an X-Cashu settle-and-refund case.

### Full unit suite

```
pytest tests/unit
→ 1584 passed, 9 failed
```

### Full integration suite

```
pytest tests/integration -m "not requires_docker"
→ 492 passed, 13 skipped, 0 failed
```

### Lint / types

```
ruff check routstr tests   → All checks passed!
mypy routstr/upstream/typesafe.py → only a pre-existing error in routstr/nostr/discovery.py
```

---

## 3. The 9 unit failures are pre-existing (proven)

Failing: `test_cashu_httpx_compat.py` (3), `test_fee_payout_migration.py` (3),
`test_mint_url_migration.py` (1), `test_provider_id_migration.py` (2).

Verified by stashing the branch (`git stash push -u`) and re-running those four
files against pristine `origin/main`:

```
→ 9 failed, 10 passed
```

Identical failures with none of this work applied. Causes:

* **httpx compat (3)** — installed httpx no longer accepts the `proxies=` kwarg
  the shim probes for; version drift, unrelated to this branch.
* **migration tests (6)** — the tests shell out to `alembic upgrade`, whose
  subprocess imports the app, whose file logger opens
  `logs/app_2026-09-19.log` — a **root-owned file created by the running
  Docker container**, unwritable by the `debian` user → `PermissionError` →
  alembic exits non-zero. Environment artifact, not code.

---

## 4. Environment note (how to reproduce these runs)

Running pytest **from the repo directory** currently fails at import:

```
ValueError: Unable to configure handler 'file'
  ← PermissionError: .../logs/app_2026-09-19.log
```

The running container (root) owns today's log file. The runs above therefore
used a scratch cwd so the logger writes elsewhere, with the repo on
`PYTHONPATH`:

```bash
mkdir -p /tmp/routstr-test && cd /tmp/routstr-test
PYTHONPATH=$HOME/projects/routstr-core \
  $HOME/projects/routstr-core/.venv/bin/python -m pytest \
  $HOME/projects/routstr-core/tests/integration -m "not requires_docker" -q
```

To run in-repo instead (`make test-unit`), either run as root, or move today's
root-owned log aside first — the container keeps writing to its open file
descriptor, so `mv` is safe and lossless:

```bash
mv logs/app_$(date -u +%F).log logs/app_$(date -u +%F).log.root-owned
```

Also note `nostr-sdk==0.45.1` was installed into `.venv` during this work (it
was missing, and blocks every import of the package).

---

## 5. Enabling it on the node (when you add the API key)

The provider table is non-empty on the live node, so env seeding won't fire —
add it explicitly:

1. Admin UI → Providers → *TypeSafe* (base URL is fixed to
   `https://api.typesafe.ai/v1`), paste your `api.typesafe.ai` key.
2. Or: `POST /admin/api/upstream-providers` with
   `{"provider_type": "typesafe", "base_url": "https://api.typesafe.ai/v1", "api_key": "<key>"}`.
3. `jev-latest` / `jev-preview` are then catalogued automatically with the
   built-in rates; override the model row if TypeSafe changes pricing.
4. Client call: `POST {node}/v1/systemone` with `{state, model, questions}`.

Containers were **not** restarted and nothing was deployed.

---

## 6. Caveats / not yet verified

* **No live call has been made** — no TypeSafe API key was available during
  this work, so the upstream contract is implemented from
  `docs.typesafe.ai` and the OpenRouter model metadata. Specifically
  unverified against the live API: the exact `GET /v1/models` envelope
  (code accepts `{"models": [...]}` and a bare list; entries keyed by `name`
  or `id`) and the `release_date` format.
* The `usage` shape (`{input_tokens, output_tokens}`) is already the canonical
  billing shape, so settlement needed no dialect handling.
* Not covered by tests: TypeSafe's `529 Overloaded` status (treated as a
  generic 5xx; single-provider failover has nothing to fall back to).
