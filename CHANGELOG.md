# Changelog

## Unreleased

- Make the periodic routing-map rebuild configurable. It was a hardcoded 60s
  loop started unconditionally, so setting `MODELS_REFRESH_INTERVAL_SECONDS=0`
  to pause model refresh still left every worker rebuilding the full mapping
  table once a minute — a provider/model `selectinload` plus a cost ranking over
  the whole catalog, for data that changes rarely. Add
  `MODEL_MAPS_REFRESH_INTERVAL_SECONDS` (default `60`, unchanged behaviour) and
  `ENABLE_MODEL_MAPS_REFRESH`; both are re-read every iteration, so pausing and
  resuming take effect without a restart. Wire up `ENABLE_MODELS_REFRESH`, which
  was defined but read nowhere, as the kill switch for fetching upstream model
  catalogs.
- Add canonical HTTP 402 payment errors with numeric millisatoshi requirements,
  spendable balance, shortfall, and reservation pricing audit trails. Preserve
  `detail.error`, X-Cashu legacy fields, and the concurrent balance alias.
  Pricing and reservation behavior are unchanged; completion/context clamping
  remains a separate proposal.
