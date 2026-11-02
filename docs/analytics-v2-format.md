# Completed-day analytics reports

Public sharing follows the node's existing analytics setting. Reports contain aggregate usage for completed UTC days. They do not contain prompts, customer identifiers, request identifiers, served upstream model identifiers, or private pricing diagnostics.

A public-sharing activation begins coverage on the next full UTC day. Toggle days are excluded. Private terminal records remain stored when public sharing is disabled. Within an eligible public period, recorded data is published even if collection was interrupted: clean downtime, crashes, stale leases and rejected outcomes do not exclude whole days or erase previously stored data. There is no downtime tolerance threshold.

These are **recorded totals, not an exhaustive request census**. An included all-zero day means no persisted contribution was found, not proof that no requests occurred. Entire weeks without persisted outcomes are not initially published; a previously published week can still receive a zero-valued correction. An omitted day is not evidence of zero activity. In-memory outcomes lost to process death cannot be reconstructed from a checkpoint or a counter.

## Operator controls and upgrades

The existing "Share analytics to Nostr" switch controls public reports through `ENABLE_ANALYTICS_SHARING`. Its existing default remains `true`; an explicitly saved `false` remains off after upgrading or restarting. There is no separate format opt-in.

Local collection starts automatically for the private dashboard and continues when public sharing is off. An upgraded node uses completed-day reports whenever its existing sharing setting is on. New coverage begins with the next full UTC day, so private history from before activation is not published. The runtime no longer starts the legacy publisher; consumers can still read legacy reports already on relays.

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

The report does not support a tokens-per-request average. `completed_requests` includes image and unknown-outcome requests, which carry no tokens, so dividing token totals by it understates usage per request. Per-request usage provenance stays in the node's private ledger and dashboard and is not published.

This unpublished format has exactly these 6 columns. Earlier 18- and 20-column drafts are not accepted published formats. A node reads its own earlier 20-column signed versions back as these 6 columns, so it can correct them after upgrading. Existing legacy public report formats remain separate inputs for consumers.

## Delivery and history

The producer publishes committed outcomes through yesterday UTC without waiting for every collector's checkpoint. Late writes and unknown-to-settled upgrades produce replacement corrections, including for closed weeks. Producer commits fence their outbox snapshot and sharing generation across workers, so a stale producer cannot overwrite a newer report. It commits the complete signed event bytes before any network send. Retries reuse those bytes across process restarts. A relay receipt requires both an affirmative `OK` for that event and readback of the complete identical signed event. A false `OK`, an acknowledgement for another ID, a forged readback with the right ID, or a missing readback does not mark delivery successful.

The operator's configured public WSS relays are the delivery targets. The producer chooses a frame size that fits the advertised limits of the required relay quorum. Missing relay information uses the 96 KiB default. A newly discovered smaller limit can produce a corrected version with more models folded into `_other`; persisted bytes are never edited. Once a correction is stored, the report it corrects is retired from sending and its bytes are kept. Delivery requires receipts from two distinct configured relay URLs, or from the single URL when only one is configured. This proves storage redundancy across URLs, not independent relay ownership. No external manifest must exist before publication can work. Disabling sharing cancels pending sends and fences older delivery generations. Disabling cannot retract data already published publicly.

The producer queries daily model groups in the database for the latest 365 completed days, expanding to the oldest week's Monday so it never truncates a weekly report. It keeps the signed history already stored. This release does not automatically delete private records or signed reports; retention remains a separate operator/product policy decision.

## Recovery and lifecycle

Only explicit sharing/identity transitions move the public boundary. Collector startup failures and temporary publication failures retain the existing generation and pending reports. Cancelling the coordinator stops its publisher, not the collector; lifespan shutdown drains collection after the server has drained requests. Storage retries retain the in-flight row and bounded queue until recovery or the shutdown deadline. A failed shutdown, rejection or lease expiry is health evidence, not grounds for suppressing other stored rows. Lease expiry means continuity is uncertain, not proof of data loss.

Rejected outcomes also leave crash-durable **loss evidence**, not a copy of the outcome: the UTC loss day, detection timestamp and random marker ID. `TERMINAL_OUTCOME_LOSS_DIR` defaults to `data/terminal-outcome-losses`; keep this directory on persistent writable storage, separate for each database/deployment. It must survive process/container replacement. Recording a new loss episode synchronously fsyncs a private, atomically renamed marker and its directory before rejection returns, including after final collector shutdown. This adds disk latency only to loss paths. Replay commits an idempotent recovered-run record before removing the marker; concurrent workers and interrupted cleanup are safe. Database outages retain markers. Disk/permission/fsync failures raise and emit a critical diagnostic rather than falsely acknowledge durable recording. Loss of the storage volume itself is outside this guarantee. No request content, identifiers, credentials or payment tokens are journaled. This does not make the normal in-memory outcome queue crash-proof.

This update does **not automatically widen historical epochs** already closed by older builds. Their rows do not retain enough identity/opt-in provenance to distinguish collector gaps from private intervals. Back up the database and establish eligibility from retained operator evidence before repair; never lower the public floor just to recover old rows. A proven eligible gap can extend a predecessor forward without overlapping its successor; already attempted frames must be corrected, never edited. Backward changes to an attempted coordinate's `coverage_start` are forbidden. Consumers must resolve corrections as replacements and must not interpret included days as lossless collection. External consumer compatibility must be checked before deployment.

The shared signed example is `tests/fixtures/analytics-v2/daily-models-signed.json`. It includes a paid model with both cache components, a free model, an unknown model, and actual zero-use days. Its three completions contain 190 recorded tokens and 3,250 msat of revenue.
