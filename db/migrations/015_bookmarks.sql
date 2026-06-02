-- Bookmarks — a separate axis from favorites.
--
-- favorites = 관심표시 (like/dislike binary). bookmarks = 실사 기록용 완충지대:
-- the user actually went and saw the room, in some order, and wants to keep
-- notes / photos / checklist on each visit. A listing can be both liked AND
-- bookmarked (one is intent, the other is a visit log) — that's why we use a
-- second table instead of another `kind` value in favorites.
--
-- Schema mirrors favorites so the wire format stays familiar:
--   - key = "{source}::{listing_no}" (same convention as favorites.key)
--   - entry_json carries the full client payload (data, note, checklist,
--     photo meta, sortOrder)
--   - sort_order column is a denormalised promotion of entry_json->>'sortOrder'
--     so we can ORDER BY it in SQL without parsing JSONB on every read.
--
-- favorite_deleted-style tombstone table — same offline-multi-device merge
-- semantics. Re-add wins if its savedAt > tombstone.deleted_at.

BEGIN;

CREATE TABLE bookmarks (
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    source          TEXT NOT NULL,
    listing_no      TEXT NOT NULL,
    listing_id      BIGINT REFERENCES listings(id) ON DELETE SET NULL,
    -- Visit order: 1 = 첫 번째 본 방. Higher = later visits. Per-user
    -- monotonic but not strictly contiguous — gaps from reorders are fine.
    sort_order      INTEGER NOT NULL DEFAULT 0,
    entry_json      JSONB NOT NULL,
    saved_at        TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

CREATE INDEX idx_bookmarks_user_order
ON bookmarks(user_id, sort_order);

CREATE INDEX idx_bookmarks_source_listing
ON bookmarks(source, listing_no);

CREATE INDEX idx_bookmarks_listing_id
ON bookmarks(listing_id) WHERE listing_id IS NOT NULL;

CREATE TABLE bookmark_deleted (
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    deleted_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE INDEX idx_bookmark_deleted_at
ON bookmark_deleted(deleted_at DESC);

COMMIT;
