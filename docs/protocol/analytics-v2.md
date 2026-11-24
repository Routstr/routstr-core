# Analytics v2, direction

**This is a direction document, not a specification.** The wire format is written and
follows in a second PR, because each decision below changes what it has to carry.

In short: v2 records durable terminal outcomes in the node, publishes daily facts inside
weekly events, waits for relay acceptance, and leaves view windows to consumers.

Nothing here is implemented. v1 stays live until a node upgrades.

Building a cost or benchmark page? Per-request cost already works today and needs none of
this, see decision 1. What v2 adds is public historical aggregates.

## What is wrong with v1

Each of these is observed in shipped code or measured on live relays on 2026-08-29.

**Events exceed the limits of relays the node already targets.** A production node
publishes a 241,929 byte frame, which exceeds the 131,072 byte `max_message_length` that
both `nos.lol` and `relay.routstr.com` advertise. On 2026-08-29 it was retrievable from
`relay.damus.io`, which permits 1,000,000, and a later oversized revision was retrievable
from `relay.ditto.pub`, which permits 4,000,000. Delivery therefore depends on whichever
large-limit relays happen to hold it. The bulk is the `windows` object at 202,055 bytes, of which the
`3m` window alone is 80,079. Nothing in the publisher checks size before sending
(`routstr/nostr/analytics.py:376-393`).

**Public resolution is hourly, and the publish times are themselves a signal.**
`model_usage_mix` is copied verbatim into the event
(`routstr/nostr/analytics.py:219`) and the 24h window is defined at one hour intervals
(`routstr/nostr/analytics.py:35-43`), so it is an hour by hour series. The
publisher wakes every 15 minutes and publishes whenever the semantic payload changed
(`routstr/nostr/analytics.py:313-318`, `:376-379`), so the timing of events reveals when
public aggregates moved.

**A snapshot cannot say what it covers.** v1 emits rolling window metadata but no
coverage-start or covered-through boundary (`routstr/nostr/analytics.py:222-229`), and it
drops zero buckets rather than emitting them
(`routstr/core/usage_analytics_store.py:1207-1212`). A quiet interval and an unreported
interval are indistinguishable.

**Usage is inferred from log files.** The store tails `app_*.log` and tracks byte offsets
(`routstr/core/usage_analytics_store.py:334`, `:453`), and reads the model name off a log
entry (`:523`). A 2026-08-29 source search found no use of `canonical_slug` anywhere in
the analytics publisher or store, so rows cannot be joined to the model catalog identity
defined at `routstr/core/db.py:446`.

**EHBP settlement is not counted.** Success is recognised only from `routstr.auth`
records carrying one of two message strings
(`routstr/core/usage_analytics_store.py:1313-1337`). EHBP settlement is emitted by a
different logger with a different shape (`routstr/upstream/ehbp.py:575-592`), so it does
not match. X-Cashu settlement needs the same check before the gap is stated precisely.

**Missing usage is invisible.** `normalize_usage` (`routstr/payment/usage.py:102`)
zero-fills absent fields, so by aggregation time "reported zero" and "never reported" are
the same value.

**Identity is ambiguous by construction.** The address is `f"{provider_id}:stats"`
(`routstr/nostr/analytics.py:382`), `PROVIDER_ID` is explicit configuration, and with it
unset the analytics and listing publishers resolve the id by different paths
(`routstr/nostr/analytics.py:59-63` versus `routstr/nostr/listing.py:289-324`). So one
signing key can hold several provider coordinates. Relays currently retain one such key,
though its second coordinate is a 2026-03-24 leftover whose provider id is a laptop
hostname, `MacBook-Pro-2.local`, not a second active node.

**Publishing never checks whether it worked.** The send path returns `True` for any socket
write that did not raise and never inspects the relay's `OK` frame
(`routstr/nostr/listing.py:340-346`). A relay answering `OK: false` is recorded as success.

## What v2 does instead

Settled earlier, from the v1 versus v2 pipeline sketch:

- **A terminal outcome ledger.** Prepaid and X-Cashu payments, in both standard and EHBP
  request modes, write one final outcome per request into one durable store. That, not logs,
  becomes the source for analytics. EHBP is a request encryption mode rather than a third
  payment method, and it composes with X-Cashu (`routstr/upstream/ehbp.py:878`).
- **A UTC bucket encoder** publishing public aggregates only.
- **Publishing awaits the relay `OK`**, with a heartbeat and per relay retry.
- **A verified last-good cache** on the consumer, newest event per `(pubkey, d)`.
- **The consumer computes its own view**: windows, freshness and coverage.

The addressing scheme in that sketch, 13 fixed addresses over 12 by 64-day slots, was
revisited once relay size limits were measured. It became **weekly
events**: a new `d` per week, the week's event replaced as it accrues, replacement stopping
once the week closes, no separate final event or kind, and a target around 48 KiB. That is
about 52 addresses and 365 or 366 ordinary daily versions per node per year, against up to
roughly 35,000 changed snapshots on the current 15-minute loop.

Three things go beyond that sketch and are proposed here:

- **One row per completed UTC day including zero days**, plus coverage metadata saying
  which days are covered and whether the week is closed, so unknown and zero stop looking
  alike.
- **Field presence captured at settlement**, before normalization discards it, so a
  consumer can tell how much of a number was actually observed.
- **Publishing never blocks inference.** A failing relay marks analytics degraded and
  retries in the background.

The principle underneath all of it: **the node publishes facts, the consumer owns policy.**

**Cost, stated honestly.** The terminal outcome ledger does not exist yet. `routstr/core/db.py`
defines ten tables and none of them records request outcomes, so this is new schema plus a
write on the settlement path, and it is the largest piece of engineering in the change.
Its per-request cost, where the daily rollup runs, and how long ledger rows are kept have
not been measured or decided.

## Decisions taken

These four shape the wire format, so they are decided here rather than left open. Each says
what was chosen and why. Say so if you disagree with any of them and I will change it before
the format is frozen. Three of the four are one-way doors once a node publishes.

**1. Revenue stays on the public wire, as daily settled totals per node and per public model
identity on the final version of a week.**

The earlier sketch placed it node-local. Carrying it instead, because v1 publishes it today
(`routstr/nostr/analytics.py:142-198`) and the landing stats page renders it as one of three
modes (`Routstr/landingpage@6317133:components/stats/stats-chart-domain.ts:13`), so dropping
it is a visible regression rather than a format change. It is also the network's clearest
supply side signal for anyone deciding whether to run a node.

The honest case against, so this is not decided on one side of the argument. Per-coordinate
revenue is commercially sensitive. It is an operator self-report, and a signature
authenticates the publisher rather than the truth of the settlement, so it can be inflated. A
sparse daily cell can reveal one customer's spend when only one request occupies it. And
this is the one decision that is reversible in only one direction: revenue can be added later
without breaking anything, but never withdrawn once published. **If you want it node-local,
now is the time, and v2 ships without it.**

Note this decides nothing about per-request cost, which already works. A caller reads it from
the `X-Routstr-Cost-Msats` response header (`routstr/upstream/base.py:143`,
`routstr/upstream/ehbp.py:308`), exposed to browsers through the CORS `expose_headers` list
in `routstr/core/main.py`. Advertised prices are already public from `/v1/models`
(`routstr/payment/models.py:683`). A benchmark that issues its own requests needs none of
this.

Refunds, payment paths, failures, request ids, API key data and customer identifiers stay
node-local either way.

**2. Provider identity is the signed `(pubkey, provider_d)` coordinate.**

The earlier sketch joined by pubkey. Using the coordinate because the protocol permits one
key to hold several provider listings, and pubkey-only analytics would collapse them into one
address with no way to separate them afterwards. The observed fleet does not need this today,
the one multi-coordinate key found carries a 2026-03-24 leftover rather than a second active
node, so the practical cost of the coordinate is zero and the cost of guessing wrong is
unrecoverable history.

**3. A completed request counts exactly once, after a successful upstream terminal state is
settled and durably committed.**

A non-streaming success, a normal stream terminal marker, or a normal EOF all count after
settlement. Missing usage still counts, with presence marked false. An upstream failure or a
reverted cancellation does not count. A client disconnect counts if completion and settlement
still succeeded. If settlement or ledger commit leaves a possible lost outcome, the node
declares a new continuity epoch rather than publishing a number it cannot prove.

**4. Public model identity is `canonical_slug or id`, taken from the candidate that actually
served after any failover, persisted at the terminal outcome.**

`ModelRow.canonical_slug` is nullable (`routstr/core/db.py:446`), so the fallback is required
rather than defensive. Today's names come off a log line and cannot be joined to the catalog
at all.


## What is not in this PR

The wire format: tag and address encoding, the content envelope, the exact column set and
ordering, the size fold ladder, correction semantics, the relay manifest, the full consumer
contract, and the signed fixture corpus that makes any of it testable. That is written and
follows once the four decisions above are confirmed.
