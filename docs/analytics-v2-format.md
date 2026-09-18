# Completed-day analytics reports

Public sharing is an explicit operator choice. Reports contain aggregate usage for completed UTC days. They do not contain prompts, customer identifiers, request identifiers, served upstream model identifiers, or private pricing diagnostics.

A public-sharing activation begins coverage on the next full UTC day. Toggle days and collection-loss days are excluded. Private terminal records remain stored when public sharing is disabled. An omitted day means unavailable coverage; an included all-zero day means the collector covered the day and recorded no completed requests.

## Signed event

- Nostr kind: `38422`.
- Content schema: `routstr.analytics.v2`.
- Provider coordinate: `38421:<signer pubkey>:<provider d>`.
- Provider hash: the first 16 lowercase hexadecimal characters of SHA256 of the provider coordinate's UTF-8 bytes.
- `d` tag: `routstr.analytics.v2:<provider hash>:week:<Monday YYYY-MM-DD>:epoch:<epoch>`.
- `a` tag: the provider coordinate.
- `w` tag: the Monday date.

The epoch distinguishes disjoint collection periods. Epochs can share a calendar week, but their covered dates must not overlap. A week is only the storage and transport grouping. Every reported day has its own totals and model partition, including elapsed days in the current week.

Consumers verify the Nostr signature, provider coordinate, content, and date bounds before accepting a report. For each full signed coordinate, the greatest `created_at` wins, with the lexicographically lower event ID breaking ties. Corrections replace that coordinate's earlier values, not add to them. Conflicting overlapping epochs for one provider and day are unavailable coverage.

## Content

`week` is the Monday date. `epoch` is a nonnegative integer. `coverage_start` and `through` are inclusive dates within that week. `complete` means that the week or collection period is closed; it does not mean that every provider in the network reported. `corrected: true` identifies a corrected version, and `corrects` points to the immediately prior published version being corrected. An ordinary version can extend `through` but cannot alter earlier daily values or model partitions.

`days` maps every date in the covered range to an integer vector. `daily_models` maps the same dates to objects mapping canonical model identifiers to vectors. Every day includes `_other`, and its named models plus `_other` sum exactly to the day's entire vector. Small-volume and free models can be named. When the signed frame would exceed its size limit, the smaller model rows are folded into `_other` without changing any daily total. Unknown models also contribute to `_other`.

The `columns` array defines these positions, in order:

| Position | Column | Meaning |
| --- | --- | --- |
| 0 | `completed_requests` | Successfully completed terminal settlements, including zero-charge completions |
| 1 | `input_observed_requests` | Requests with reported input usage |
| 2 | `output_observed_requests` | Requests with reported output usage |
| 3 | `cache_read_observed_requests` | Requests with reported cache-read usage |
| 4 | `cache_creation_observed_requests` | Requests with reported cache-creation usage |
| 5 | `input_tokens` | Reported or estimated input tokens |
| 6 | `output_tokens` | Reported or estimated output tokens |
| 7 | `cache_read_input_tokens` | Reported or estimated cache-read tokens |
| 8 | `cache_creation_input_tokens` | Reported or estimated cache-creation tokens |
| 9 | `revenue_msats` | Gross settled node revenue in millisatoshis |
| 10 | `input_estimated_requests` | Requests with estimated input usage |
| 11 | `output_estimated_requests` | Requests with estimated output usage |
| 12 | `cache_read_estimated_requests` | Requests with estimated cache-read usage |
| 13 | `cache_creation_estimated_requests` | Requests with estimated cache-creation usage |
| 14 | `input_missing_requests` | Requests without input usage |
| 15 | `output_missing_requests` | Requests without output usage |
| 16 | `cache_read_missing_requests` | Requests without cache-read usage |
| 17 | `cache_creation_missing_requests` | Requests without cache-creation usage |
| 18 | `measured_token_requests` | Requests eligible for the measured-token average |
| 19 | `measured_tokens` | Input, output, and cache tokens from exactly those eligible requests |

For each input/output/cache component, reported + estimated + missing requests equals completed requests. Missing usage is not a measured zero. Every integer is nonnegative and no greater than JavaScript's maximum safe integer. Days and daily model rows must each satisfy these rules independently.

The measured-token cohort requires reported input and output on the same request. Reported cache counts are included; missing cache counts of zero mean no recorded cache contribution. An estimated cache field, or a missing cache field with a positive count, excludes that request. Normalized input excludes cache tokens, so measured tokens add input, output, cache-read, and cache-creation exactly once. A reported zero-token completion remains eligible, including a free completion.

The measured-token average is `measured_tokens / measured_token_requests`, or unavailable when the count is zero. Both fields are computed together from eligible requests. Separate input/output missing counters cannot reconstruct this cohort. Its request count cannot exceed either reported input or reported output request count; its tokens cannot exceed all four token totals; zero eligible requests require zero measured tokens. These conditions also hold for every model row and survive folding into `_other`.

Explicit estimated usage remains estimated even if an older observed flag is true. Explicit reported usage stays reported. Otherwise, a true observed flag supplies reported provenance for older records whose source field is missing or unset. Records without either indication stay missing. The private reader and public producer use the same normalization.

This unpublished format has exactly 20 columns. The earlier 18-column draft is not an accepted published format. Existing legacy public report formats remain separate inputs for consumers.

## Delivery and history

The producer waits until every active collector has drained its queue past the reported UTC midnight. It commits the complete signed event bytes before any network send. Retries reuse those bytes across process restarts. A relay receipt requires both an affirmative `OK` for that event and readback of the complete identical signed event. A false `OK`, an acknowledgement for another ID, a forged readback with the right ID, or a missing readback does not mark delivery successful.

The operator's configured public WSS relays are the delivery targets. The producer chooses a frame size that fits the advertised limits of the required relay quorum. Missing relay information uses the 96 KiB default. A newly discovered smaller limit can produce a corrected version with more models folded into `_other`; persisted bytes are never edited. Once a correction is stored, the report it corrects is retired from sending and its bytes are kept. Delivery requires receipts from two distinct configured relay URLs, or from the single URL when only one is configured. This proves storage redundancy across URLs, not independent relay ownership. No external manifest must exist before publication can work. Disabling sharing cancels pending sends and fences older delivery generations. Disabling cannot retract data already published publicly.

The producer queries daily model/source/cohort groups in the database for the latest 365 completed days, expanding to the oldest week's Monday so it never truncates a weekly report. Grouping keeps eligible requests separate from missing-positive-cache requests so the measured numerator and denominator remain matched. It keeps the signed history already stored. This release does not automatically delete private records or signed reports; retention remains a separate operator/product policy decision.

The shared signed example is `tests/fixtures/analytics-v2/daily-models-signed.json`. It includes a paid model with reported usage and both cache components, a free model with estimated usage, an unknown model with missing usage, and actual zero-use days. Its three completions contain 190 recorded tokens and 3,250 msat of revenue. One completion qualifies for the measured average, with 170 tokens including 6 cache-read and 4 cache-creation tokens.
