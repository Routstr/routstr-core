# Changelog

## Unreleased

- Compress and revalidate the model catalog: `GET /v1/models` and
  `GET /v1/models/paths` now honour `Accept-Encoding: gzip` (~670 KB of JSON
  for a few hundred models becomes ~85 KB), carry an `ETag` with
  `Vary: Accept-Encoding`, and answer a matching `If-None-Match` with a
  bodyless `304`. The JSON body is unchanged for clients that do not accept
  gzip. This keeps the polling clients (every routstrd daemon, every 21
  minutes, on a 10 s deadline) from timing out against nodes with large
  catalogs.

- Add canonical HTTP 402 payment errors with numeric millisatoshi requirements,
  spendable balance, shortfall, and reservation pricing audit trails. Preserve
  `detail.error`, X-Cashu legacy fields, and the concurrent balance alias.
  Pricing and reservation behavior are unchanged; completion/context clamping
  remains a separate proposal.
