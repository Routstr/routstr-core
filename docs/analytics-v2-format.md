# Completed-day analytics reports

Public sharing follows the node's existing analytics setting. Reports contain aggregate usage for completed UTC days. They do not contain prompts, customer identifiers, request identifiers, served upstream model identifiers, or private pricing diagnostics.

A public-sharing activation begins coverage on the next full UTC day. Toggle days are excluded. Private terminal records remain stored when public sharing is disabled. Within an eligible public period, recorded data is published even if collection was interrupted: clean downtime, crashes, stale leases and rejected outcomes do not exclude whole days or erase previously stored data. There is no downtime tolerance threshold.

These are **recorded totals, not an exhaustive request census**. An included all-zero day means no persisted contribution was found, not proof that no requests occurred. Entire weeks without persisted outcomes are not initially published; a previously published week can still receive a zero-valued correction. An omitted day is not evidence of zero activity. In-memory outcomes lost to process death cannot be reconstructed from a checkpoint or a counter.

## Coverage and restarts

A clean service shutdown stops serving and drains request settlement before closing the collector. Restarting collection preserves coverage regardless of downtime length, including overnight and multi-day maintenance. There is no same-day or ten-minute cutoff. A collector that fails to start does not block serving: the node retries it, and outcomes it cannot accept meanwhile are recorded as dropped loss evidence. Cancelling the publication coordinator does not stop collection. Detached stream settlement is drained before the collector may close cleanly; incomplete settlement leaves loss evidence instead of a clean close.

An unclean process stop can lose in-memory queued outcomes. Public reports still publish the rows that were recorded. The private dashboard lists the uncertain days, from the last proven collection checkpoint through recovery, in `ledger_coverage.incomplete_days`. Persisting the queue during in-process recovery does not make it survive process death.

Another collector or a startup can fence a writer whose heartbeat lease expired. Lease expiry means continuity is uncertain, not that an outcome was proven dropped. The private dashboard's `ledger_coverage.loss_diagnostics` distinguishes `lease_expired` (uncertain continuity), `continuity_lost` (the writer itself declared continuity uncertain without proving a drop, such as a writer failure, a lost lease, or stream settlement that did not finish), and `outcome_dropped` (a confirmed drop: an outcome was discarded because the writer was unavailable, its queue was full, the outcome was invalid or rejected by storage, its idempotency key conflicted with a stored outcome, or shutdown discarded it from the queue). Historical rows and replayed loss markers have no recorded reason and are reported as `unknown`, which remains uncertain. These diagnostics are not a complete drop audit: outcomes lost to process death leave only the expired lease.

## Operator controls and upgrades

The existing "Share analytics to Nostr" switch controls public reports through `ENABLE_ANALYTICS_SHARING`. Its existing default remains `true`; an explicitly saved `false` remains off after upgrading or restarting. There is no separate format opt-in.

Upgrade disclosure: legacy sharing already published named-model request counts, revenue and combined token totals. V2 can name models previously folded into `others`, publishes per-model input/output/cache composition, and retains daily partitions where the legacy one-year window used weekly buckets. Recent legacy hourly buckets were finer than V2 daily buckets. Existing sharing consent is reused; changing the default alone would not change a saved `true`. Operators who do not want this disclosure should disable sharing before upgrading.

Local collection starts automatically for the private dashboard and continues when public sharing is off. An upgraded node uses completed-day reports whenever its existing sharing setting is on. New coverage begins with the next full UTC day, so private history from before activation is not published unless the operator explicitly releases it (see below). The runtime no longer starts the legacy publisher; consumers can still read legacy reports already on relays.

A provider's signing identity must remain stable, and an identity change creates a new publication transition. Turning sharing off stops pending delivery but cannot retract reports already published.

## Signed event

- Nostr kind: `38422`.
- Content schema: `routstr.analytics.v2`.
- Provider coordinate: `38421:<signer pubkey>:<provider d>`. The provider d is 1 to 64 characters without control or surrogate characters.
- Provider hash: the first 16 lowercase hexadecimal characters of SHA256 of the provider coordinate's UTF-8 bytes.
- `d` tag: `routstr.analytics.v2:<provider hash>:week:<Monday YYYY-MM-DD>:epoch:<epoch>`.
- `a` tag: the provider coordinate.
- `w` tag: the Monday date.

The epoch distinguishes disjoint publication periods, not collector uptime. Epochs can share a calendar week, but their covered dates must not overlap: the producer refuses to sign an epoch's days on or before the last day any earlier epoch covers. A week is only the storage and transport grouping. Every reported day has its own totals and model partition, including elapsed days in the current week.

Consumers verify the Nostr signature, provider coordinate, content, and date bounds before accepting a report. For each full signed coordinate, the greatest `created_at` wins, with the lexicographically lower event ID breaking ties. Corrections replace that coordinate's earlier values, not add to them. Conflicting overlapping epochs for one provider and day are unavailable coverage.

## Content

`week` is the Monday date. `epoch` is a nonnegative integer no greater than JavaScript's maximum safe integer, so it reads back exactly from both the `d` tag and the content. `coverage_start` and `through` are inclusive dates within that week. `complete` means that the week or publication period is closed; it does not guarantee that every request was collected or every provider reported. Closed periods can still be corrected when late rows or settlement updates arrive. `corrected: true` identifies a corrected version, and `corrects` points to the immediately prior published version being corrected. An ordinary version can extend `through`, or keep it and set `complete` when the collection period closes, but cannot alter earlier daily values or model partitions.

`days` maps every date in the covered range to an integer vector. `daily_models` maps the same dates to objects mapping canonical model identifiers to vectors. Every day includes `_other`, and its named models plus `_other` sum exactly to the day's entire vector. Small-volume and free models can be named. When the signed frame would exceed its size limit, the smaller model rows are folded into `_other` without changing any daily total. Unknown models also contribute to `_other`.

The `columns` array defines these positions, in order:

| Position | Column | Meaning |
| --- | --- | --- |
| 0 | `completed_requests` | Billed requests, including zero-charge completions, interrupted streams, image requests, and requests whose settlement outcome is unknown |
| 1 | `input_tokens` | Billed input tokens, excluding cache tokens |
| 2 | `output_tokens` | Billed output tokens |
| 3 | `cache_read_input_tokens` | Billed cache-read tokens |
| 4 | `cache_creation_input_tokens` | Billed cache-creation tokens |
| 5 | `revenue_msats` | Recorded gross retained revenue in millisatoshis; unknown-settlement rows contribute only ecash known to be retained, a lower bound rather than guaranteed final revenue |

Token counts are the ones billing used: the upstream's reported usage where it sent one, otherwise the node's estimate. Every integer is nonnegative and no greater than JavaScript's maximum safe integer. Days and daily model rows must each satisfy these rules independently.

The report does not support a tokens-per-request average. `completed_requests` includes image and unknown-outcome requests, which carry no tokens, so dividing token totals by it understates usage per request. Per-request usage provenance stays in the node's private ledger and dashboard and is not published. Public reports do not identify which completions have unknown settlement outcomes, so consumers cannot distinguish their lower-bound revenue contributions from final settled amounts.

This unpublished format has exactly these 6 columns. Earlier 18- and 20-column drafts are not accepted published formats. A node reads its own earlier 20-column signed versions back as these 6 columns, so it can correct them after upgrading. Existing legacy public report formats remain separate inputs for consumers.

## Delivery and history

The producer publishes committed outcomes through yesterday UTC without waiting for every collector's checkpoint. Late writes and unknown-to-settled upgrades produce replacement corrections, including for closed weeks. Producer commits fence their outbox snapshot and sharing generation across workers, so a stale producer cannot overwrite a newer report. It commits the complete signed event bytes before any network send. Retries reuse those bytes across process restarts. A relay receipt requires both an affirmative `OK` for that event and readback of the complete identical signed event. A false `OK`, an acknowledgement for another ID, a forged readback with the right ID, or a missing readback does not mark delivery successful.

The operator's configured public WSS relays are the delivery targets. A report is delivered once **one** configured relay accepts the frame and serves the identical signed event back; failure or silence on the other configured relays does not withhold it. Recounting is not a durability precondition: the receipt already proves the exact bytes are retrievable, and every accepting relay still gets its own receipt, so redundancy across URLs remains observable and auditable. Frame sizing is deliberately separate from that threshold — the producer sizes a frame to fit the advertised limits of the two best relays (or all targets when fewer are configured), so one relay with a small maximum does not force unnecessary `_other` folding, while no target is ever sent more than it advertised. Missing relay information uses the 96 KiB default. A newly discovered smaller limit can produce a corrected version with more models folded into `_other`; persisted bytes are never edited. Once a correction is stored, the report it corrects is retired from sending and its bytes are kept. Delivery requires a receipt from at least one configured relay URL, backed by an affirmative `OK` and readback of the identical event. Additional receipts are recorded opportunistically as evidence of redundancy across URLs, not independent relay ownership, but are not required to mark the report delivered. No external manifest must exist before publication can work. Disabling sharing cancels pending sends and fences older delivery generations. Disabling cannot retract data already published publicly.

The producer queries daily model groups in the database for the latest 365 completed days, expanding to the oldest week's Monday so it never truncates a weekly report. It keeps the signed history already stored. This release does not automatically delete private records or signed reports; retention remains a separate operator/product policy decision.

## Recovery and lifecycle

Only explicit sharing/identity transitions move the public boundary. Collector startup failures and temporary publication failures retain the existing generation and pending reports. Cancelling the coordinator stops its publisher, not the collector; lifespan shutdown drains collection after the server has drained requests. Storage retries retain the in-flight row and bounded queue until recovery or the shutdown deadline. A failed shutdown, rejection or lease expiry is health evidence, not grounds for suppressing other stored rows. Lease expiry means continuity is uncertain, not proof of data loss.

Rejected outcomes also leave crash-durable **loss evidence**, not a copy of the outcome: the UTC loss day, detection timestamp and random marker ID. `TERMINAL_OUTCOME_LOSS_DIR` defaults to `data/terminal-outcome-losses`; keep this directory on persistent writable storage, separate for each database/deployment. It must survive process/container replacement. Recording a new loss episode synchronously fsyncs a private, atomically renamed marker and its directory before rejection returns, including after final collector shutdown. This adds disk latency only to loss paths. Replay commits an idempotent recovered-run record before removing the marker; concurrent workers and interrupted cleanup are safe. Database outages retain markers. Disk/permission/fsync failures raise and emit a critical diagnostic rather than falsely acknowledge durable recording. Loss of the storage volume itself is outside this guarantee. No request content, identifiers, credentials or payment tokens are journaled. This does not make the normal in-memory outcome queue crash-proof.

This update does **not automatically widen historical epochs** already closed by older builds. Their rows do not retain enough identity/opt-in provenance to distinguish collector gaps from private intervals. Back up the database and establish eligibility from retained operator evidence before repair; never lower the public floor just to recover old rows. A proven eligible gap can extend a predecessor forward without overlapping its successor; already attempted frames must be corrected, never edited. Backward changes to an attempted coordinate's `coverage_start` are forbidden. Consumers must resolve corrections as replacements and must not interpret included days as lossless collection. External consumer compatibility must be checked before deployment.

### Publishing older history

An operator can choose to publish a closed epoch that was never published, typically the private collection from before sharing was first turned on. The admin settings list these epochs (`GET /admin/api/analytics-sharing/history`) and publish one through `POST /admin/api/analytics-sharing/history/release` with the epoch numbers and the current delivery generation.

An epoch qualifies only when it is closed, below the active public floor, and has no outbox row under any identity, so epochs that were once published (then opted out) never return. The release belongs to the current generation: any later sharing transition or identity rotation withdraws it, and any week already published stays as it is. Released epochs keep their original numbers and coverage, which end before the floor epoch begins, so the per-day ownership rule and epoch ordering are unchanged; a week shared with the floor epoch gets one report per epoch with disjoint days. The 365-day public history window still applies. A released epoch that fails validation is skipped and logged without blocking the current epoch.

The shared signed example is `tests/fixtures/analytics-v2/daily-models-signed.json`. It includes a paid model with both cache components, a free model, an unknown model, and actual zero-use days. Its three completions contain 190 recorded tokens and 3,250 msat of revenue.


## Private admin analytics API compatibility

The dashboard combines **recorded terminal outcomes** with separately labelled **log diagnostics**. These are different populations: logs can rotate, and some recorded settlements do not have corresponding success logs. Never calculate a success rate by dividing ledger outcomes by logged requests. `successful_chat_completions` (and model-row `successful`) are retained API names for all recorded outcomes, including interrupted streams, image requests, zero-charge requests and unknown settlement outcomes; the UI labels them **Recorded Outcomes**. `unknown_outcome_requests` counts rows whose pricing source is `unknown`, not rows with an unknown model. It is included in summaries, metric buckets/totals and model rows without changing existing revenue or token denominators.

Each `summary`, `metrics`, and `revenue_by_model` object carries additive metadata, including when returned by its standalone endpoint:

- `field_provenance` maps data-field names to `terminal_outcomes`, `logs`, or `unavailable`. In metrics it describes bucket/totals measures; in revenue-by-model it describes model-row measures and totals. In particular, model `requests`, `failed` and `refunds_sats` are log diagnostics, while `successful`, revenue and average revenue come from the ledger.
- `request_count_semantics` is `recorded_outcomes_including_unknown_settlements`.
- `revenue_semantics` is `recorded_gross_retained_lower_bound_for_unknown_settlements`. Unknown settlements contribute only ecash known to be retained, not guaranteed final revenue.
- `deprecated_fields` maps legacy aliases to their replacement fields. Summary `net_revenue_msats`/`net_revenue_sats`, and model `net_revenue_sats`, are deprecated aliases of `revenue_msats`/`revenue_sats`. **They no longer mean legacy log revenue minus reverted holds.** Clients must migrate to the named revenue fields and check their semantics. Do not subtract reverted authorization holds from recorded retained revenue to reconstruct a net figure.

Explicit calendar ranges are UTC day selections. Arbitrary historical log diagnostics are not available; `ledger_coverage.diagnostic_available` is false and log-field provenance is `unavailable`. Their legacy zero placeholders are not observed zero activity. These requests bypass legacy indexing/aggregation entirely. Rolling presets continue to include log diagnostics.

Error-detail objects retain the legacy offset-free, server-local `timestamp` for compatibility and add ISO-8601 `timestamp_utc` (with `Z`, or null for an invalid timestamp). The UI uses the UTC companion to align errors with charts and explicitly marks older-server fallback values as server local. Existing local log stamps have no offset: a duplicated DST hour cannot be disambiguated retrospectively and retains the existing first-occurrence interpretation. This release does not rewrite stored logs or change their schema.

## Control-loop and identity safety

The coordinator continues to refresh the database sharing fence and settings every second, so cross-worker opt-outs and identity rotations retain their existing responsiveness. Unchanged encrypted identities reuse decrypted/derived key material; ciphertext or identity-state changes invalidate that cache. Settings refresh validates values before mutating live fields, and settings saves bound compare-and-swap attempts; repeated conflicts return HTTP 409 and require retrying the save.

Identity discovery distinguishes complete, truncated and failed relay reads. Only a matching end-of-stored-events response establishes a complete page. A full page remains conservatively truncated: deduplicating its visible provider coordinates cannot rule out another coordinate outside the page. Without an explicit `PROVIDER_ID`, the coordinate is the one signed listing `d` tag the reachable relays show, or else the first 12 hex characters of the node's public key, which is the same fallback the listing publisher announces. Unreachable relays are skipped, so one dead relay in the list does not pause stats; when no relay is reachable, sharing pauses and retries instead of claiming the fallback over a listing it could not see. Two different coordinates, or truncated history that could hide one, still pause sharing until the operator configures `PROVIDER_ID`. The resolved coordinate is stored with the delivery state and reused, so it does not drift once claimed. Listing and report validators consistently reject control and surrogate characters.
