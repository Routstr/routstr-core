# Changelog

## Unreleased

- Add canonical HTTP 402 payment errors with numeric millisatoshi requirements,
  spendable balance, shortfall, and reservation pricing audit trails. Preserve
  `detail.error`, X-Cashu legacy fields, and the concurrent balance alias.
  Pricing and reservation behavior are unchanged; completion/context clamping
  remains a separate proposal.
