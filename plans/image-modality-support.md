# OpenRouter image modality support

## Implemented scope

Branch `feat/image-modality-support`, worktree `.worktrees/image-modality-support`.

- Native buffered `POST /v1/images`, opt-in, funded bearer/account authentication.
- Consolidated OpenRouter text/image/embedding catalogue discovery, best-effort bounded image endpoint enrichment.
- Reusable endpoint capability metadata with freshness, unit-labelled prices, variants and provider routing identities (`routstr/modalities.py`).
- Nullable persisted metadata and Alembic migration `8d71c5a2f903`; ordinary admin writes cannot forge endpoint metadata.
- Decimal fixed-per-image quotes, conservative maximum variant rates, provider pinning and no fallback after dispatch.
- Completion-based upstream USD settlement supports cost-only usage; frozen FX/markup, reservation cap, existing authoritative/idempotent account settlement.
- Dedicated bounded transport, no automatic generation retries, cancellation reservation cleanup.
- Catalogue only advertises safely quoteable image-only models when enabled; no image-only chat routing even during capability outages.
- Documentation, dry-run-first smoke client and credential-free isolated Podman fake API.

## Explicit limitations

Not complete OpenRouter image/modality parity. Token-priced image models (including Flare), megapixel/request-unit pricing, direct image X-Cashu redemption, streaming, EHBP, multipart, provider passthrough and model-path overrides remain unsupported. Token/megapixel quantities lack verified enforceable universal bounds. Cashu generation is rejected before redemption until durable refund failure recovery is implemented. Missing authoritative response cost produces an error and releases the reservation rather than estimating chat tokens or charging a hold.

USD admission ceiling is not an upstream spending cap. Future modalities require their own enforceable quantity/reservation and completion contracts.

## Verification

Final runs in the worktree:

- Unit suite excluding wallet file: 2393 passed, 1 skipped, 5 deselected.
- Wallet unit file run separately: 114 passed.
- Mocked integration suite: 583 passed, 10 skipped, 7 deselected.
- Ruff checks on changed/new Python files and `git diff --check`: passed.
- Credential-free fake API Podman smoke: passed; ephemeral container removed, no global pruning.
- Independent focused review: three defects fixed and regression-tested (raw malformed duplicate provider tags, client alias/upstream identity, image-only chat rejection during metadata outage); no remaining findings in rereview.

Authenticated live verification on 2026-10-08: one real OpenRouter generation through the actual Routstr ASGI `/v1/images` route, using an isolated SQLite account seeded only for this test, real bearer authentication, live capability discovery, live BTC/USD conversion, real transport and account settlement. `recraft/recraft-v4.1-flash` returned HTTP 200 and a valid 1024×1024 WebP (500448 bytes). Routstr billed USD 0.00742 (USD 0.007 upstream reported usage plus 6% markup), 8936 msats; account debit matched and reserved balance returned to zero. Immediate OpenRouter credits usage delta was zero, so account-ledger posting was not independently confirmed; settlement used response usage cost. Key loaded from the user-supplied dotenv file without displaying or tracking it. Generated image and disposable DB remain outside the repository under `/tmp`. Post-live focused regression: 108 passed. Full Routstr container deployment remains untested.

## Enablement

See `docs/provider/openrouter-images.md` and `.env.example`. Configure an OpenRouter provider through admin, run normal DB migrations, set `IMAGE_GENERATION_ENABLED=true` and a positive `IMAGE_MAX_REQUEST_USD`, restart/refresh discovery and use a funded Routstr bearer account. Recommended first live smoke candidate: `recraft/recraft-v4.1-flash` (public listed base price USD 0.007/image at research time), subject to fresh discovery and pricing.

## Next work

1. Deployment smoke using a normally funded account and a full Routstr container (isolated ASGI live generation and settlement already passed).
2. Establish provider-specific enforceable bounds for token/megapixel image models before enabling them.
3. Durable Cashu refund recovery before direct image Cashu payments.
4. Dedicated image SSE completion/cancellation settlement.
5. Extend reusable capability metadata with independently tested adapters for remaining modalities.
