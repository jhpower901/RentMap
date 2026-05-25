# RentMap DB History Schema

Date: 2026-05-24

First database shape for moving RentMap from daily CSV files toward hourly
listing history. Storage-frugal by design — target is well under hundreds of
MB per year of cumulative growth, with no raw HTML or JSON payloads stored.

## Decisions

- One normalized schema for every platform instead of separate platform
  tables.
- `listings` is the platform listing identity (UPSERT on `(platform_id,
  platform_listing_id)`).
- `listing_snapshots` is **incremental only** — insert one row only when
  `content_hash` differs from the previous snapshot for that listing. No
  per-run row for "seen unchanged."
- `listing_price_snapshots` is **incremental only** — insert one row only
  when `price_hash` changes. This is the time-series source for price trend
  charts; gaps in time imply "unchanged".
- No `listing_presence` table. The "seen in the latest crawl" signal lives
  in `listings.last_seen_at` + `last_crawl_run_id` + `miss_count`. A
  per-hour-per-listing presence table was rejected — it would produce
  ~9 M rows/month, blowing the storage budget.
- No `listing_raw_pages` table. Original payloads are explicitly not stored.
  If a future feature needs them, ship to object storage with a short
  retention policy (≤ days), not Postgres.
- Platform-specific fields go in `listing_snapshots.raw_normalized_json`
  for now; this column has no GIN index because it is not a hot query path.
- `listing_status_events` carries the webhook dispatch queue directly
  (`webhook_sent_at` / `_attempts` / `_next_try_at` / `_last_error`), so
  there is no separate outbox table to maintain.

## Migrations

- `db/migrations/001_core_listing_history.sql`
  - `platforms` (seeded with the four sources)
  - `crawl_runs`
  - `listings`
  - `listing_snapshots`
  - `listing_price_snapshots`
  - `listing_status_events`

## Storage Budget

Estimated steady-state monthly growth, assuming ~1 % daily price-change rate
and ~5 % daily new-listing rate against an active inventory of ~12 k listings
across the four sources:

| table                     | rows / month | bytes / row | size / month |
|---------------------------|--------------|-------------|--------------|
| listings (net change)     | ~4 k         | ~200        | ~10 MB (cum.)|
| listing_snapshots         | ~6 k         | ~500        | ~3 MB        |
| listing_price_snapshots   | ~3 k         | ~150        | ~0.5 MB      |
| listing_status_events     | ~9 k         | ~300        | ~3 MB        |
| crawl_runs                | ~2.9 k       | ~200        | ~0.6 MB      |
| **monthly delta**         |              |             | **~20 MB**   |

Indexes roughly double the on-disk footprint, so plan for ~50 MB/month and
~500 MB after a year. The biggest unknown is `raw_normalized_json`; the
ingestion code is expected to leave it empty (`{}`) for the common case and
only fill it for fields with no normalized home.

## Ingestion Flow

1. Insert one `crawl_runs` row per (platform, hourly crawl). Track started_at
   first; fill finished_at + counts at the end.
2. For each row in the crawl result:
   1. Upsert `listings` by `(platform_id, platform_listing_id)`. New rows
      seed `first_seen_at = now()`; existing rows refresh `last_seen_at`,
      `last_crawl_run_id`, and reset `miss_count` to 0.
   2. Build normalized JSON for the row and calculate three hashes:
      - `content_hash` — stable hash of all normalized fields
      - `price_hash` — stable hash of price fields only
      - `detail_hash` — stable hash of non-price detail fields
   3. Compare against the latest snapshot for this listing.
   4. If `content_hash` changed (or no prior snapshot exists), insert
      `listing_snapshots`. Otherwise skip — the listing is "seen unchanged"
      and presence is implicit from `last_seen_at`.
   5. If `price_hash` changed, insert `listing_price_snapshots` referencing
      the just-inserted snapshot row.
   6. Emit `listing_status_events` for `discovered`, `price_changed`,
      `detail_changed`. Each event row is a webhook outbox entry.
3. After the per-row loop, find listings missing from this run for the same
   platform (`active`/`missing` with `last_crawl_run_id` ≠ current run id):
   - Increment `miss_count`.
   - If `miss_count = 1..2` → set `current_status = 'missing'` and keep it
     as a quiet retry queue entry for the same scheduler run. No webhook event
     is emitted.
   - The scheduler immediately reruns the crawler up to two more times. If a
     listing is still `missing` after those in-schedule retries, it finalizes
     the row as `removed`, sets `removed_at`, and emits `removed`.
4. For any `removed`/`missing` listing that reappears in a later run, set
   `current_status = 'active'`, `removed_at = NULL`, `reappeared_at = now()`,
   and emit `reappeared` event. The price/snapshot trail continues seamlessly
   because `listings.id` is stable.
5. Close out `crawl_runs.finished_at` and the per-event counters.

## Missing / Removed Policy

- Seen in the current run: `active`, `miss_count = 0`.
- Missing once or twice inside the same scheduler run: `missing`,
  `miss_count = 1..2` (quiet retry queue; no alert yet).
- Still missing after the scheduler's two immediate retries: `removed`
  (deletion is recorded as `removed_at`, the row is **not** deleted).
- A `missing` listing recovered during those retries is quietly patched back to
  `active`. A previously `removed` listing seen again becomes `active` plus a
  `reappeared` event.

## Deferred Tables

These are intentionally deferred until ingestion is stable and a real need
appears:

- `agents` (and `agent_snapshots`) — flatten into snapshots for now.
- `listing_images` — image URLs survive in `listing_snapshots.raw_normalized_json`.
- `listing_options` / `option_types` — keep `options` as semicolon-joined
  text in snapshots; normalize only when analytics requires it.
- `canonical_properties` / `canonical_match_candidates` — cross-platform
  matching is a project of its own. `listings.canonical_property_id` is
  reserved as a NULL column to avoid future ALTERs.
- `listing_raw_pages` — see above; storing raw bodies is out of scope.
