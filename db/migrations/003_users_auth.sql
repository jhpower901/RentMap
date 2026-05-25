-- Self-hosted login & per-user data isolation.
--
-- Until now access has been gated solely by Caddy's HTTP Basic Auth, and every
-- piece of user state (favorites, photos, area-filter polygon) was global.
-- This migration introduces a users + sessions model and prepares favorites /
-- favorite_deleted for per-user scoping. The actual NOT NULL promotion + PK
-- swap happens in 004, *after* the operator runs
-- ``python scripts/users.py migrate-globals --to <admin>`` to backfill
-- user_id on the existing global rows.

BEGIN;

-- CITEXT lets us treat usernames case-insensitively without LOWER() on every
-- read. Cheap to add; idempotent.
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE users (
    id              BIGSERIAL PRIMARY KEY,
    username        CITEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    display_name    TEXT,
    is_admin        BOOLEAN NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMPTZ
);

-- Server-side session store. Cookie holds only the opaque token id; lookup
-- joins back here so an admin can revoke a single session (or all sessions
-- for a user) by deleting rows. Tokens are generated as
-- ``secrets.token_urlsafe(32)`` — 32 bytes of randomness in the id column.
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_agent      TEXT,
    ip              INET
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);

-- One polygon per user. The client also keeps a localStorage copy for offline
-- read; on load it pulls the server copy, on edit it debounces a PUT. There is
-- no conflict-resolution beyond "last writer wins on updated_at" — single user
-- across devices, not collaborative editing.
CREATE TABLE user_area_filters (
    user_id         BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    points_json     JSONB NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Per-user scoping for existing tables ────────────────────────────────────
-- user_id is nullable for now so the migration applies cleanly on a populated
-- DB. The data backfill (scripts/users.py migrate-globals) fills it in, and
-- 004_favorites_user_required.sql promotes it to NOT NULL plus swaps the PK.
ALTER TABLE favorites
    ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE favorite_deleted
    ADD COLUMN user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;

CREATE INDEX idx_favorites_user ON favorites(user_id);
CREATE INDEX idx_favorite_deleted_user ON favorite_deleted(user_id);

COMMIT;
