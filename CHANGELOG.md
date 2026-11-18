# Changelog

## Unreleased

- Cap the periodic platform-fee payout at the primary mint's conservative
  owner surplus, the same bound the operator payout path already uses. The dev
  cut can no longer melt proofs that back user balances and drive a mint's
  owner balance negative; anything the surplus does not cover stays accrued
  and is retried later.
- Add canonical HTTP 402 payment errors with numeric millisatoshi requirements,
  spendable balance, shortfall, and reservation pricing audit trails. Preserve
  `detail.error`, X-Cashu legacy fields, and the concurrent balance alias.
  Pricing and reservation behavior are unchanged; completion/context clamping
  remains a separate proposal.
