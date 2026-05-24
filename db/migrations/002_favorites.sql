-- Favorites + deletion tombstones, moved from sqlite (data/rentmap.db) so all
-- shared state lives in one Postgres. Schema mirrors the JSON shape the
-- existing /api/favorites endpoints serve, so the wire format and client
-- code don't change.
--
-- ``key`` is the canonical identity (``{source}::{listing_no}`` joined by the
-- client). source values are platform codes as the *client* sees them — that
-- includes 'manual' for user-entered listings, so we don't FK to platforms.
-- The optional ``listing_id`` column lets us join to ``listings`` for the
-- crawled sources (and stays NULL for manual entries).
--
-- favorite_deleted is a tombstone table — merge logic uses (deleted_at vs
-- saved_at) timestamps to decide whether a re-add should win or stay
-- suppressed, so we can't just hard-delete rows.

BEGIN;

CREATE TABLE favorites (
    key             TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    listing_no      TEXT NOT NULL,
    -- Optional FK so favorited rows can be joined to the live listing for
    -- enrichment. Resolved lazily on insert (server side) and on demand.
    listing_id      BIGINT REFERENCES listings(id) ON DELETE SET NULL,
    -- Full client payload (listing data, rating, notes, photos meta, ...).
    -- JSONB so we can grow client fields without touching the schema.
    entry_json      JSONB NOT NULL,
    saved_at        TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_favorites_source_listing
ON favorites(source, listing_no);

CREATE INDEX idx_favorites_saved_at
ON favorites(saved_at DESC);

CREATE INDEX idx_favorites_listing_id
ON favorites(listing_id) WHERE listing_id IS NOT NULL;

CREATE TABLE favorite_deleted (
    key             TEXT PRIMARY KEY,
    deleted_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_favorite_deleted_at
ON favorite_deleted(deleted_at DESC);

COMMIT;
