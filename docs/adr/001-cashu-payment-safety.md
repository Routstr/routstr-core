# ADR-001: Cashu payment safety boundaries

## Status

Accepted

## Context

Cashu proofs are bearer instruments. Retrying quote creation, refund delivery, or a dispatched Lightning melt can duplicate side effects or spend proofs whose outcome is still unknown. Mint transport failures and concurrent workers also need one shared policy.

## Decision

- Treat account, invoice, quote, melt, token-delivery, and refund creation as non-idempotent unless an upstream idempotency key is available.
- A dispatched melt with an unknown outcome keeps a durable quote-linked proof reservation until later reconciliation confirms a terminal state. An immediate `unpaid` observation after transport loss is not terminal.
- Size melts from the quote amount, reserve, and exact proof input fees within the caller's gross budget; do not use recursive send selection for melt planning.
- Apply mint transport/rate cooldowns centrally and permit only explicit reconciliation probes during cooldown.
- Auto-topups require fresh threshold confirmation, durable per-provider claims/cooldown, atomic spend-cap checks, and owner-only funds.

## Consequences

Transient failures can delay payouts/topups rather than risk duplicate payment. Operators may need to reconcile ambiguous claims. Tests must cover restart, concurrency, and partial-stream failures at these boundaries.
